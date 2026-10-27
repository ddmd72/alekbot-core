"""The shared evaluating Firestore fake: stores documents and evaluates what a repository sends (H1, TEST_COVERAGE_SYSTEMIC_RFC §3).

Standalone and collection-agnostic (the drive suite keeps its own fake in `tests/unit/adapters/firestore_drive_eval_fakes.py`;
this module imports nothing from it). Documents are nested dicts. Evaluated, not recorded:

- queries: `where` (keyword `filter=` or positional `field, op, value`, validated as the client does: `None` only with
  `==` / `!=`, which become IS_NULL / IS_NOT_NULL) with `==`, null, `!=` (a document without the field or with a null
  is excluded), ranges, `in`, `array_contains*`; Firestore's value-type ordering; the implicit ordering (a range or
  `!=` filter orders by its field after the explicit orderings, in the last explicit direction, then `__name__`, so it
  decides what a `limit` returns); a naive `datetime` filter value (also inside an `in` list) is the UTC instant, as
  the client encodes it (cursor values and datetimes nested in a map value are compared as given); cursors; `limit`; `select`; aggregations; cosine `find_nearest` (any other measure
  is refused) whose pre-filter may be a range or `!=`; dotted field paths everywhere (`where`, `order_by`, `select`,
  cursors, `snap.get`);
- writes: `set` (merge or not), `create` (`AlreadyExists`), `update` (dotted keys are nested paths, `NotFound` on a
  missing document, optional `last_update_time` precondition), `delete`, batches; `Increment`, `ArrayUnion`,
  `ArrayRemove`, `SERVER_TIMESTAMP` (a tz-aware UTC `datetime`) and `DELETE_FIELD` applied; `set`/`create`/`update`
  return a `WriteResult` whose `transform_results` follow the alphabetical order of the transformed paths; values are
  stored as the client encodes them (a naive `datetime` is UTC, an aware one is converted to UTC, tuples and sets come
  back as lists, a `datetime.date` raises `TypeError`); a batch or a transaction commit is atomic: when any write
  fails (`AlreadyExists`, `NotFound`, a failed precondition) none is applied;
- paths: subcollections (`doc.collection(name)`, `db.collection("a/b/c")`), `list_documents()`, auto-id
  `collection.document()`, `db.collection_group(name)`;
- transactions: `eval_transactions()` patches `google.cloud.firestore.async_transactional` with the fake's decorator.
  Reads before writes (a read after a write raises `ReadAfterWriteError`), writes buffered and applied at commit, a
  commit aborts and the body re-runs when a document it read was changed by another transaction's commit (5 attempts,
  then the real client's `ValueError`). An outside non-transactional write during the body is NOT detected: real
  Firestore blocks such a write until the transaction ends, here it simply applies and the transaction commits over it.
  `dispatching_transactions()` (installed for the whole session by the autouse fixture in `tests/conftest.py`) patches a
  decorator that runs the fake's loop for an `EvalTransaction` and the original decorator, captured at install time, for
  any other transaction; code that decorates inside its method therefore works on `EvalDb` in every test. Interference
  hooks for tests: `EvalDb.interfere_on_txn_read` (another transaction commits to a document at the moment a transaction
  reads it), `EvalDb.interfere_on_txn_query` (the same, right after a transactional query returned the document) and `EvalDb.raise_on_txn_read` (the read raises once, as a read-time `Aborted` does on real Firestore).
  `EvalDb.get_all` yields in the reverse of the input order: real Firestore promises no order, so a caller must pair by
  `snap.id`;
- opt-in index refusal: `EvalDb(indexes=..., groups=...)` raises `FailedPrecondition` for a query that needs a
  composite index no declared index serves, for a vector query without a declared vector index on its field, and for
  a collection-group query with a filter or ordering that no COLLECTION_GROUP index (composite or non-empty field
  override) covers; a bare collection-group stream needs none.

Not a Firestore emulator: only what the repositories in this codebase use. Known gaps, deliberately not built:
a write that changes nothing bumps `update_time` here while real Firestore keeps it (harmless where every write sets a
timestamp field of its own); a read-time `Aborted` is modelled only through `raise_on_txn_read`; an `update_time` token
is an int here (one DB-wide stamp per commit, shared by every write of a batch or transaction commit) and a
`DatetimeWithNanoseconds` on the real client, so compare it for equality only, never order it;
`list_documents()` lists stored documents only (real Firestore also lists "missing" parents that have subcollections);
transactions give no phantom protection (a query that returned nothing is not protected, so a check-then-set such as
`firestore_user_repo.link_platform_identity` is not serialised, while real transactions are serializable); single-field
index exemptions (`"indexes": []` overrides) in the config are ignored, so a query they would make unservable is not
refused; a transaction used outside the decorator is not refused. Every executed query on a collection is logged in
`EvalCollection.executed` (`("where", field, op, value)`, `("order_by", field, direction)`, `("limit", n)`, ...).
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, InvalidArgument, NotFound, Aborted
from google.cloud.firestore_v1 import ReadAfterWriteError
from google.cloud.firestore_v1._helpers import READ_AFTER_WRITE_ERROR, GeoPoint
from google.cloud.firestore_v1.base_document import BaseDocumentReference
from google.cloud.firestore_v1.base_query import _validate_opation
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.transforms import (
    DELETE_FIELD, SERVER_TIMESTAMP, ArrayRemove, ArrayUnion, Increment,
)
from google.cloud.firestore_v1.types import StructuredQuery
from google.cloud.firestore_v1.types.document import Value
from google.cloud.firestore_v1.types.write import WriteResult
from google.cloud.firestore_v1.vector import Vector

__all__ = [
    "EvalDb", "EvalCollection", "EvalDocRef", "EvalSnap", "EvalQuery", "EvalTransaction", "WriteOption",
    "eval_transactions", "dispatching_transactions", "stored_vector",
]

_MISSING = object()
_DISJUNCTIONS_MAX = 30          # Firestore: values per `in` / `array_contains_any`, and the disjunction product
_ARRAY_OPS = ("array_contains", "array_contains_any")
_EQUALITY_OPS = ("==", "IS_NULL", "in")
_RANGE_OPS = ("<", "<=", ">", ">=", "!=", "not-in", "IS_NOT_NULL")   # the inequality filters
_MAX_ATTEMPTS = 5               # google.cloud.firestore_v1.base_transaction.MAX_ATTEMPTS


def stored_vector(value: Any) -> List[float]:
    """A stored `Vector` (or list) as a plain list of floats, for assertions."""
    return [float(x) for x in value]


# --------------------------------------------------------------------------------------------- field paths

def _get_path(data: Optional[Dict[str, Any]], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = target[part] = {}
        target = nxt
    target[leaf] = value


def _del_path(target: Dict[str, Any], path: str) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        target = target.get(part)    # type: ignore[assignment]
        if not isinstance(target, dict):
            return
    target.pop(leaf, None)


def _project(data: Dict[str, Any], paths: Optional[List[str]]) -> Dict[str, Any]:
    """The document with only `paths` (dotted paths come back nested, as Firestore returns them)."""
    if paths is None:
        return copy.deepcopy(data)
    out: Dict[str, Any] = {}
    for path in paths:
        value = _get_path(data, path)
        if value is not _MISSING:
            _set_path(out, path, copy.deepcopy(value))
    return out


# ---------------------------------------------------------------------------------- ordering and matching

def _rank(value: Any) -> int:
    """Firestore's order of value types: null < bool < NaN < number < timestamp < string < bytes < reference < geo point
    < array < vector < map (verified on the test database 2026-10-22)."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1
    if isinstance(value, float) and math.isnan(value):
        return 2
    if isinstance(value, (int, float)):
        return 3
    if isinstance(value, datetime):
        return 4
    if isinstance(value, str):
        return 5
    if isinstance(value, bytes):
        return 6
    if isinstance(value, (BaseDocumentReference, EvalDocRef)):
        return 7
    if isinstance(value, GeoPoint):
        return 8
    if isinstance(value, (list, tuple)):
        return 9
    if isinstance(value, Vector):
        return 10
    return 11


def _key(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return (9, tuple(_key(v) for v in value))
    rank = _rank(value)
    if rank == 7:
        return (rank, value.path if isinstance(value, BaseDocumentReference) else f"{value.col.name}/{value.id}")
    if rank == 8:
        return (rank, (value.latitude, value.longitude))
    return (rank, value if rank in (1, 3, 4, 5, 6) else 0)


class _Desc:
    """Inverts the order of a key, for DESCENDING orderings inside one tuple."""

    def __init__(self, key: Any) -> None:
        self.key = key

    def __lt__(self, other: "_Desc") -> bool:
        return other.key < self.key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Desc) and other.key == self.key


def _equal(got: Any, value: Any) -> bool:
    return _rank(got) == _rank(value) and got == value


def _match(data: Dict[str, Any], field: str, op: str, value: Any) -> bool:
    got = _get_path(data, field)
    if got is _MISSING:
        return False                      # every filter, `!=` included, excludes a document without the field
    if op == "IS_NULL":
        return got is None
    if op == "==":
        return _equal(got, value)
    if op == "IS_NOT_NULL":
        return got is not None
    if op == "!=":
        # Firestore docs: the field must exist with a value other than the given one, null excluded.
        # a value of another type counts as "not equal" (verified on the test database 2026-10-22)
        return got is not None and not _equal(got, value)
    if op in ("<", "<=", ">", ">="):
        if got is None or _rank(got) != _rank(value):
            return False
        return {"<": got < value, "<=": got <= value, ">": got > value, ">=": got >= value}[op]
    if op == "in":
        return any(_equal(got, v) for v in value)
    if op == "array_contains":
        return isinstance(got, list) and value in got
    if op == "array_contains_any":
        return isinstance(got, list) and any(v in got for v in value)
    raise AssertionError(f"unsupported operator {op}")


def _check_shape(ops: List[tuple]) -> None:
    """Refuse what Firestore refuses with INVALID_ARGUMENT: a value list over 30, two array filters in one query and a
    disjunction product (`in` x `array_contains_any`) over 30. (A kNN pre-filter may be a range or `!=`.)"""
    wheres = [op for op in ops if op[0] == "where"]
    product = 1
    for _, field, op, value in wheres:
        if op in ("in", "array_contains_any"):
            if len(value) > _DISJUNCTIONS_MAX:
                raise InvalidArgument(f"'{op}' on {field} takes at most {_DISJUNCTIONS_MAX} values, got {len(value)}")
            product *= len(value)
    if product > _DISJUNCTIONS_MAX:
        raise InvalidArgument(f"the query has {product} disjunctions, more than {_DISJUNCTIONS_MAX}")
    if sum(op in _ARRAY_OPS for _, _, op, _ in wheres) > 1:
        raise InvalidArgument("at most one array-contains or array-contains-any filter per query")


# --------------------------------------------------------------------------------------- write transforms

Sink = List[Tuple[str, Value]]


def _resolve(value: Any, current: Any, path: str, sink: Sink) -> Any:
    """A written value with its transforms applied over `current`; each transform adds its result to `sink`."""
    if isinstance(value, Increment):
        base = current if isinstance(current, (int, float)) and not isinstance(current, bool) else 0
        total = base + value.value
        sink.append((path, Value(integer_value=total) if isinstance(total, int) else Value(double_value=total)))
        return total
    if isinstance(value, ArrayUnion):
        merged = list(current) if isinstance(current, list) else []
        merged += [v for v in value.values if v not in merged]
        sink.append((path, Value(null_value=0)))     # array transforms report a null result (verified on the test database 2026-10-22)
        return merged
    if isinstance(value, ArrayRemove):
        kept = [v for v in current if v not in value.values] if isinstance(current, list) else []
        sink.append((path, Value(null_value=0)))     # array transforms report a null result (verified on the test database 2026-10-22)
        return kept
    if value is SERVER_TIMESTAMP:
        now = datetime.now(timezone.utc)
        sink.append((path, Value(timestamp_value=now)))
        return now
    if isinstance(value, dict):                      # a map replaces what was there; transforms inside it still apply
        return {k: _resolve(v, None, f"{path}.{k}", sink) for k, v in value.items()}
    return _plain(value)


def _plain(value: Any) -> Any:
    """A written value as the client encodes it and a read decodes it (`_helpers.encode_value` / `decode_value`)."""
    if isinstance(value, datetime):                  # naive means UTC; an aware value comes back as the UTC instant
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, date):
        raise TypeError("Cannot convert to a Firestore Value", value, "Invalid type", type(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return copy.deepcopy(value)


def _filter_value(value: Any) -> Any:
    """A `where` value as the client sends it: `_helpers.encode_value` turns a naive `datetime` into the UTC instant
    (`google.cloud._helpers._microseconds_from_datetime`), also inside an `in` / `array_contains_any` list, so it
    compares with the stored aware values instead of raising `TypeError`. Every other value is kept as given (cursor
    values are not encoded here). Parity scenario `naive_datetime_filter_is_utc`."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, (list, tuple)):
        return type(value)(_filter_value(v) for v in value)
    return value


def _merge(target: Dict[str, Any], data: Dict[str, Any], prefix: str, sink: Sink) -> None:
    """`set(merge=True)`: maps merge recursively, `DELETE_FIELD` removes, anything else replaces."""
    for name, value in data.items():
        path = f"{prefix}{name}"
        if value is DELETE_FIELD:
            target.pop(name, None)
        elif isinstance(value, dict):
            sub = target.get(name)
            if not isinstance(sub, dict):
                sub = {}
            _merge(sub, value, f"{path}.", sink)
            target[name] = sub
        else:
            target[name] = _resolve(value, target.get(name), path, sink)


def _result(sink: Sink) -> WriteResult:
    ordered = [value for _, value in sorted(sink, key=lambda item: item[0])]
    return WriteResult(update_time=datetime.now(timezone.utc), transform_results=ordered)


# ------------------------------------------------------------------------------------------- snapshots, refs

class WriteOption:
    """What `client.write_option(last_update_time=...)` returns in the fake."""

    def __init__(self, last_update_time: Any = None) -> None:
        self.last_update_time = last_update_time


class EvalSnap:
    def __init__(self, ref: "EvalDocRef", data: Optional[Dict[str, Any]]) -> None:
        self.reference = ref
        self.id = ref.id
        self.exists = data is not None
        self.update_time = ref.col.versions.get(ref.id, 0)
        self._data = data

    def to_dict(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field_path: str) -> Any:
        """One field by (dotted) path: `None` for a document that does not exist, `KeyError` for a missing field."""
        if self._data is None:
            return None
        value = _get_path(self._data, field_path)
        if value is _MISSING:
            raise KeyError(field_path)
        return copy.deepcopy(value)


class EvalDocRef:
    def __init__(self, col: "EvalCollection", doc_id: str) -> None:
        self.col, self.id = col, doc_id

    def __deepcopy__(self, memo: Any) -> "EvalDocRef":
        return self     # a reference is a handle: stored and read back as the same one, never a copy of the database

    @property
    def doc_id(self) -> str:
        return self.id

    def collection(self, name: str) -> "EvalCollection":
        return self.col.db.collection(f"{self.col.name}/{self.id}/{name}")

    async def get(self, transaction: Any = None, field_paths: Optional[List[str]] = None) -> EvalSnap:
        interference = None
        if transaction is not None:
            transaction._check_read_allowed()
            self.col.db._raise_if_queued(self)
            interference = self.col.db._next_interference(self)
            if interference is not None and not interference[1]:
                self.col.db._interfere(self, interference[0])
            transaction._note_read(self)
        data = self.col.docs.get(self.id)
        snap = EvalSnap(self, None if data is None else _project(data, field_paths))
        if interference is not None and interference[1]:
            self.col.db._interfere(self, interference[0])
        if self.col.yield_on_get:
            await asyncio.sleep(0)   # concurrent callers all hold the same (soon stale) snapshot
        return snap

    async def create(self, data: Dict[str, Any]) -> WriteResult:
        return self._apply("create", data)

    async def set(self, data: Dict[str, Any], merge: Any = False) -> WriteResult:
        return self._apply("set", data, merge)

    async def update(self, data: Dict[str, Any], option: Optional[WriteOption] = None) -> WriteResult:
        return self._apply("update", data, option)

    async def delete(self, option: Optional[WriteOption] = None) -> None:
        self._apply("delete", None, option)

    def _check_precondition(self, option: Optional[WriteOption]) -> None:
        wanted = None if option is None else option.last_update_time
        # a `last_update_time` precondition on a missing document fails as FailedPrecondition, before any existence check
        # (verified on the test database 2026-10-22)
        if wanted is not None and (self.id not in self.col.docs or self.col.versions.get(self.id, 0) != wanted):
            raise FailedPrecondition(f"Document changed: {self.col.name}/{self.id}")

    def _apply(self, kind: str, data: Optional[Dict[str, Any]] = None, extra: Any = None,
               by_transaction: bool = False, stamp: Optional[int] = None) -> WriteResult:
        """The one place a document changes: single writes, batches and transaction commits all end here. `stamp` is the
        commit's token (one per batch or transaction commit); a single write takes a new one."""
        col = self.col
        if stamp is None:
            stamp = col.db._tick()
        sink: Sink = []
        if kind == "create":
            if self.id in col.docs:
                raise AlreadyExists(f"Document exists: {col.name}/{self.id}")
            stored: Dict[str, Any] = {k: _resolve(v, None, k, sink) for k, v in data.items()}   # type: ignore[union-attr]
        elif kind == "set":
            # `set` keys are literal field names, a dotted key is NOT a nested path, unlike `update` (verified on the test database 2026-10-22)
            if extra is True:
                stored = copy.deepcopy(col.docs.get(self.id, {}))
                _merge(stored, data, "", sink)               # type: ignore[arg-type]
            elif extra:
                stored = copy.deepcopy(col.docs.get(self.id, {}))
                for path in extra:
                    value = _get_path(data, path)             # type: ignore[arg-type]
                    if value is _MISSING:
                        _del_path(stored, path)
                    else:
                        _set_path(stored, path, _resolve(value, _get_path(stored, path), path, sink))
            else:
                stored = {k: _resolve(v, None, k, sink) for k, v in data.items()}   # type: ignore[union-attr]
        elif kind == "update":
            self._check_precondition(extra)
            if self.id not in col.docs:
                raise NotFound(f"No document to update: {col.name}/{self.id}")
            stored = copy.deepcopy(col.docs[self.id])
            for path, value in data.items():                  # type: ignore[union-attr]
                if value is DELETE_FIELD:
                    _del_path(stored, path)
                else:
                    _set_path(stored, path, _resolve(value, _get_path(stored, path), path, sink))
        elif kind == "delete":
            self._check_precondition(extra)
            col.docs.pop(self.id, None)
        else:
            raise AssertionError(f"unknown write {kind}")
        if kind != "delete":
            col.docs[self.id] = stored
        col.versions[self.id] = stamp
        if by_transaction:
            col.txn_versions[self.id] = col.txn_versions.get(self.id, 0) + 1
        return _result(sink)


# ------------------------------------------------------------------------------------------------ queries

class EvalQuery:
    def __init__(self, db: "EvalDb", cols: List["EvalCollection"], ops: List[tuple], group: Optional[str] = None) -> None:
        self.db, self.cols, self.ops, self.group = db, cols, ops, group

    def _then(self, op: tuple) -> "EvalQuery":
        return EvalQuery(self.db, self.cols, self.ops + [op], self.group)

    def where(self, field_path: Optional[str] = None, op_string: Optional[str] = None, value: Any = None,
              *, filter: Any = None) -> "EvalQuery":
        if filter is not None:
            op = filter.op_string
            op = op if isinstance(op, str) else StructuredQuery.UnaryFilter.Operator(op).name
            return self._then(("where", filter.field_path, op, _filter_value(filter.value)))
        assert field_path is not None and op_string is not None, "where() takes a field, an operator and a value"
        op = _validate_opation(op_string, value)    # the client's check: None / NaN only with == and !=
        op = op if isinstance(op, str) else StructuredQuery.UnaryFilter.Operator(op).name
        return self._then(("where", field_path, op, _filter_value(value)))

    def order_by(self, field: str, direction: str = "ASCENDING") -> "EvalQuery":
        return self._then(("order_by", field, direction))

    def limit(self, n: int) -> "EvalQuery":
        return self._then(("limit", n))

    def select(self, paths: Any) -> "EvalQuery":
        return self._then(("select", tuple(str(p) for p in paths)))

    def start_after(self, values: Any) -> "EvalQuery":
        """A dict of the leading orderings' values, or a document snapshot (the client's snapshot cursor)."""
        return self._then(("start_after", values if isinstance(values, EvalSnap) else dict(values)))

    def find_nearest(self, **kw: Any) -> "EvalQuery":
        return self._then(("find_nearest", kw))

    def count(self, alias: Optional[str] = None) -> "EvalQuery":
        return self._then(("count", alias))

    def sum(self, field: str, alias: Optional[str] = None) -> "EvalQuery":
        return self._then(("sum", field, alias))

    async def get(self, transaction: Any = None) -> List[Any]:
        if transaction is not None:
            transaction._check_read_allowed()
        if self.group is None:
            self.cols[0].executed.append(self.ops)
        _check_shape(self.ops)
        self.db._check_index(self.cols[0].name if self.group is None else self.group, self.group is not None, self.ops)
        if self.group is None and self.cols[0].fail is not None:
            raise self.cols[0].fail
        result = self._evaluate()
        if transaction is not None:
            for snap in result:
                if isinstance(snap, EvalSnap):
                    transaction._note_read(snap.reference)
            for snap in result:
                if isinstance(snap, EvalSnap):
                    queued = self.db._query_interference.get((snap.reference.col.name, snap.reference.id))
                    if queued:
                        self.db._interfere(snap.reference, queued.pop(0))
        return result

    async def stream(self, transaction: Any = None):   # type: ignore[no-untyped-def]
        for snap in await self.get(transaction=transaction):
            yield snap

    def _evaluate(self) -> List[Any]:
        group = self.db._group_collections(self.group) if self.group is not None else self.cols
        rows = [(f"{col.name}/{doc_id}", col, doc_id, data) for col in group for doc_id, data in col.docs.items()]
        rows.sort(key=lambda row: row[0])
        orders: List[tuple] = []
        cursor: Optional[Dict[str, Any]] = None
        limit: Optional[int] = None
        paths: Optional[List[str]] = None
        nearest: Optional[Dict[str, Any]] = None
        aggregate: Optional[tuple] = None
        for op in self.ops:
            if op[0] == "where":
                rows = [r for r in rows if _match(r[3], op[1], op[2], op[3])]
            elif op[0] == "order_by":
                orders.append((op[1], op[2]))
            elif op[0] == "start_after":
                cursor = op[1]
            elif op[0] == "limit":
                limit = op[1]
            elif op[0] == "select":
                paths = list(op[1])
            elif op[0] == "find_nearest":
                nearest = op[1]
            elif op[0] in ("count", "sum"):
                aggregate = op
        if nearest is not None:
            return self._nearest(rows, nearest, paths)
        last = orders[-1][1] if orders else "ASCENDING"
        ordered = {name for name, _ in orders}
        # the implicit ordering of inequality filters; several fields sort lexicographically by field path
        # (verified on the test database 2026-10-22)
        implicit = {f for _, f, op, _ in (o for o in self.ops if o[0] == "where") if op in _RANGE_OPS and f not in ordered}
        orders.extend((field, last) for field in sorted(implicit))
        for name, _ in orders:   # an ordering excludes documents without the field
            rows = [r for r in rows if _get_path(r[3], name) is not _MISSING]

        def sort_key(row: tuple) -> tuple:
            parts = [(_Desc(_key(_get_path(row[3], n))) if d == "DESCENDING" else _key(_get_path(row[3], n)))
                     for n, d in orders]
            parts.append(_Desc(row[0]) if last == "DESCENDING" else row[0])   # implicit __name__
            return tuple(parts)

        rows.sort(key=sort_key)
        if cursor is not None:
            # a snapshot cursor: its value for every ordering (implicit ones too) and its path as the `__name__` tiebreak
            snapshot = cursor if isinstance(cursor, EvalSnap) else None
            if snapshot is not None:
                for n, _ in orders:
                    if _get_path(snapshot._data, n) is _MISSING:
                        raise ValueError(f'The "order by" field path {n!r} is not present in the cursor data {snapshot._data!r}.')
                cursor = {n: _get_path(snapshot._data, n) for n, _ in orders}
            names = list(cursor)
            assert names == [n for n, _ in orders][:len(names)], (names, orders)

            def part(d: str, value: Any) -> Any:
                return _Desc(_key(value)) if d == "DESCENDING" else _key(value)

            def tie(path: str) -> tuple:
                return ((_Desc(path) if last == "DESCENDING" else path),) if snapshot is not None else ()

            at = tuple(part(d, cursor[n]) for n, d in orders[:len(names)]) + tie(f"{snapshot.reference.col.name}/{snapshot.id}" if snapshot else "")
            rows = [r for r in rows
                    if at < tuple(part(d, _get_path(r[3], n)) for n, d in orders[:len(names)]) + tie(r[0])]
        if aggregate is not None:
            if aggregate[0] == "count":
                return [[SimpleNamespace(alias=aggregate[1], value=len(rows[:limit] if limit else rows))]]
            values = [_get_path(r[3], aggregate[1]) for r in rows]
            total = sum(v or 0 for v in values if v is not _MISSING)
            return [[SimpleNamespace(alias=aggregate[2], value=total)]]
        if limit is not None:
            rows = rows[:limit]
        return [EvalSnap(EvalDocRef(col, doc_id), _project(data, paths)) for _, col, doc_id, data in rows]

    def _nearest(self, rows: List[tuple], spec: Dict[str, Any], paths: Optional[List[str]]) -> List[EvalSnap]:
        if spec.get("distance_measure") != DistanceMeasure.COSINE:
            raise AssertionError(f"the fake computes COSINE distance only, got {spec.get('distance_measure')!r}")
        field, query = spec["vector_field"], [float(x) for x in spec["query_vector"]]
        result_field = spec.get("distance_result_field")
        threshold = spec.get("distance_threshold")
        scored = []
        for path, col, doc_id, data in rows:
            vector = _get_path(data, field)
            if not isinstance(vector, Vector):
                continue
            v = [float(x) for x in vector]
            dot = sum(a * b for a, b in zip(v, query))
            na, nb = math.sqrt(sum(a * a for a in v)), math.sqrt(sum(b * b for b in query))
            distance = 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)
            if threshold is not None and distance > threshold:
                continue
            scored.append((distance, path, col, doc_id, data))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = []
        for distance, _, col, doc_id, data in scored[:spec["limit"]]:
            shown = copy.deepcopy(data)
            if result_field:
                _set_path(shown, result_field, distance)
            out.append(EvalSnap(EvalDocRef(col, doc_id), _project(shown, paths)))
        return out


class EvalCollection(EvalQuery):
    """A collection is the query with no operations; `name` is its full path (`skills/u1/versions`)."""

    def __init__(self, name: str, db: "EvalDb") -> None:
        super().__init__(db, [self], [])
        self.name = name
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.versions: Dict[str, int] = {}        # the DB-wide commit stamp of the last write: the snapshot's `update_time`
        self.txn_versions: Dict[str, int] = {}    # bumped by transaction commits only: what aborts a transaction
        self.executed: List[List[tuple]] = []
        self.fail: Optional[Exception] = None
        self.yield_on_get = False

    def document(self, doc_id: Optional[str] = None) -> EvalDocRef:
        return EvalDocRef(self, doc_id or uuid.uuid4().hex[:20])

    async def list_documents(self, page_size: Optional[int] = None):   # type: ignore[no-untyped-def]
        for doc_id in sorted(self.docs):
            yield self.document(doc_id)


# ------------------------------------------------------------------------------------- batches, transactions

class EvalBatch:
    def __init__(self, db: "EvalDb") -> None:
        self.db = db
        self.writes: List[tuple] = []

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self.writes.append(("set", ref, data, merge))

    def update(self, ref: EvalDocRef, data: Dict[str, Any], option: Optional[WriteOption] = None) -> None:
        self.writes.append(("update", ref, data, option))

    def delete(self, ref: EvalDocRef, option: Optional[WriteOption] = None) -> None:
        self.writes.append(("delete", ref, None, option))

    async def commit(self) -> None:
        assert len(self.writes) <= 500, "Firestore takes at most 500 writes per batch"
        self.db._commit_writes(self.writes)
        self.db.commits.append(list(self.writes))


class EvalTransaction:
    """Reads are recorded, writes buffered; `eval_transactions()`' decorator commits and retries (see the module doc)."""

    def __init__(self, db: "EvalDb", max_attempts: int = _MAX_ATTEMPTS) -> None:
        self.db, self.max_attempts = db, max_attempts
        self._reset()

    def _reset(self) -> None:
        self._reads: Dict[Tuple[str, str], int] = {}
        self._writes: List[tuple] = []

    def _check_read_allowed(self) -> None:
        if self._writes:
            raise ReadAfterWriteError(READ_AFTER_WRITE_ERROR)

    def _note_read(self, ref: EvalDocRef) -> None:
        self._check_read_allowed()
        self._reads.setdefault((ref.col.name, ref.id), ref.col.txn_versions.get(ref.id, 0))

    def create(self, ref: EvalDocRef, data: Dict[str, Any]) -> None:
        self._writes.append(("create", ref, data, None))

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self._writes.append(("set", ref, data, merge))

    def update(self, ref: EvalDocRef, data: Dict[str, Any], option: Optional[WriteOption] = None) -> None:
        self._writes.append(("update", ref, data, option))

    def delete(self, ref: EvalDocRef, option: Optional[WriteOption] = None) -> None:
        self._writes.append(("delete", ref, None, option))

    def _commit(self) -> None:
        for (name, doc_id), seen in self._reads.items():
            if self.db.cols[name].txn_versions.get(doc_id, 0) != seen:
                raise Aborted(f"Document changed by another transaction: {name}/{doc_id}")
        self.db._commit_writes(self._writes, by_transaction=True)
        self._reset()


def _fake_async_transactional(fn: Any) -> Any:
    """The fake's `firestore.async_transactional`: the real decorator's loop (retry on an aborted commit, then the
    real `ValueError`), over the evaluating transaction."""

    async def run(transaction: EvalTransaction, *args: Any, **kwargs: Any) -> Any:
        last: Optional[Aborted] = None
        try:
            for _ in range(transaction.max_attempts):
                transaction._reset()
                result = await fn(transaction, *args, **kwargs)
                try:
                    transaction._commit()
                    return result
                except Aborted as exc:
                    last = exc
            raise ValueError(f"Failed to commit transaction in {transaction.max_attempts:d} attempts.") from last
        except BaseException:
            transaction._reset()
            raise

    return run


@contextmanager
def eval_transactions() -> Iterator[None]:
    """The one global patch: every `@firestore.async_transactional` in `src/` is looked up at call time on the
    `google.cloud.firestore` module, so this swaps in the evaluating decorator for the `with` block. The installed
    object is a fresh wrapper per call, never the one a previous block or the session fixture installed."""
    def fresh(fn: Any) -> Any:
        return _fake_async_transactional(fn)

    with patch("google.cloud.firestore.async_transactional", new=fresh):
        yield


def _dispatching_decorator(original: Any) -> Any:
    def decorate(fn: Any) -> Any:
        fake = _fake_async_transactional(fn)

        async def run(transaction: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(transaction, EvalTransaction):
                return await fake(transaction, *args, **kwargs)
            return await original(fn)(transaction, *args, **kwargs)    # a decorated copy per call: no shared retry state

        return run

    return decorate


@contextmanager
def dispatching_transactions() -> Iterator[None]:
    """Patch `google.cloud.firestore.async_transactional` with a decorator that runs the fake's loop for an
    `EvalTransaction` and the decorator found at install time for any other transaction."""
    from google.cloud import firestore

    with patch("google.cloud.firestore.async_transactional", new=_dispatching_decorator(firestore.async_transactional)):
        yield


# --------------------------------------------------------------------------------------------------- indexes

def _load_indexes(indexes: Union[None, str, Path, List[dict]]) -> Optional[List[Tuple[str, str, List[tuple]]]]:
    """(collection group, scope, [(field, mode)]) per declared composite; a field-override (exemption) is skipped and
    a trailing `__name__` entry is dropped (it only fixes the direction of the implicit tiebreak)."""
    if indexes is None:
        return None
    raw = json.loads(Path(indexes).read_text()) if isinstance(indexes, (str, Path)) else indexes
    declared = []
    for idx in raw:
        if "fields" not in idx:     # a field override: its index entries are single-field indexes (an empty list = exemption, ignored)
            for entry in idx.get("indexes", []):
                declared.append((idx["collectionGroup"], entry.get("queryScope", "COLLECTION"),
                                 [(idx["fieldPath"], entry.get("order") or ("CONTAINS" if "arrayConfig" in entry else "VECTOR"))]))
            continue
        fields = [(f["fieldPath"], f.get("order") or ("CONTAINS" if "arrayConfig" in f else "VECTOR"))
                  for f in idx["fields"]]
        if fields and fields[-1][0] == "__name__":
            fields.pop()
        declared.append((idx["collectionGroup"], idx.get("queryScope", "COLLECTION"), fields))
    return declared


def _required_index(ops: List[tuple]) -> Optional[Tuple[set, List[tuple]]]:
    """(head, tail) of the composite a query needs, or None when Firestore's single-field indexes serve it.
    head: the equality / `in` / array fields, in any order; tail: the orderings in order, then the range fields not
    already ordered (a vector query: the vector field, with every pre-filter field in the head). Rules: filters on
    several fields that are all equality / `in` / `array_contains*` (Firestore merges the single-field indexes) and
    any single-field filter or ordering need no composite; a second field next to a range / `!=` or an ordering, or a
    second ordering, does; a vector query needs its vector index, with or without a pre-filter."""
    wheres = [op for op in ops if op[0] == "where"]
    orders = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
    nearest = [op[1] for op in ops if op[0] == "find_nearest"]
    head = {(f, "CONTAINS" if op in _ARRAY_OPS else "EQ") for _, f, op, _ in wheres if op in _EQUALITY_OPS + _ARRAY_OPS}
    range_fields = [f for _, f, op, _ in wheres if op in _RANGE_OPS]
    if nearest:     # a vector query always needs a declared vector index on its field (never created automatically)
        return {(f, "CONTAINS" if op in _ARRAY_OPS else "EQ") for _, f, op, _ in wheres}, [(nearest[0]["vector_field"], "VECTOR")]
    fields = {f for _, f, _, _ in wheres} | {n for n, _ in orders}
    # equality + array-contains(-any) merge without a composite (verified on the test database 2026-10-22)
    if len(fields) < 2 or not (orders or range_fields):
        return None
    ordered = {n for n, _ in orders}
    # the range fields follow in lexicographic field-path order, as the implicit ordering does, whatever order the filters were added in
    tail = list(orders) + [(f, "ASCENDING") for f in sorted(set(range_fields) - ordered)]
    return head, tail


def _serves(declared: List[tuple], head: set, tail: List[tuple]) -> bool:
    cut = len(declared) - len(tail)
    if cut < 0:
        return False
    have_head, have_tail = declared[:cut], declared[cut:]
    # Firestore does not serve a composite in reverse either: exact directions only (verified on the test database 2026-10-22)
    return (have_tail == tail
            and {(f, "CONTAINS" if m == "CONTAINS" else "EQ") for f, m in have_head} == head)


# ------------------------------------------------------------------------------------------------------ db

class EvalDb:
    def __init__(self, indexes: Union[None, str, Path, List[dict]] = None,
                 groups: Optional[Dict[str, str]] = None) -> None:
        """`indexes` (a path to `config/firestore.indexes.json` or its parsed list) switches the index refusal on;
        `groups` maps a collection's name here to the collection group the index file declares it under (an unmapped
        collection is looked up under its own last path segment)."""
        self.cols: Dict[str, EvalCollection] = {}
        self.commits: List[List[tuple]] = []
        self._clock = 0
        self._interference: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], bool]]] = {}
        self._query_interference: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._raises: Dict[Tuple[str, str], List[BaseException]] = {}
        self.declared = _load_indexes(indexes)
        self.groups = dict(groups or {})

    def write_option(self, last_update_time: Any = None) -> WriteOption:
        return WriteOption(last_update_time)

    def collection(self, name: str) -> EvalCollection:
        if name not in self.cols:
            self.cols[name] = EvalCollection(name, self)
        return self.cols[name]

    def collection_group(self, name: str) -> EvalQuery:
        return EvalQuery(self, [], [], group=name)

    def _group_collections(self, name: str) -> List[EvalCollection]:
        return [c for c in self.cols.values() if c.name.rsplit("/", 1)[-1] == name]

    def batch(self) -> EvalBatch:
        return EvalBatch(self)

    def _tick(self) -> int:
        """The next commit stamp: DB-wide and increasing, so two documents written in different commits never share a
        snapshot token (compare for equality only)."""
        self._clock += 1
        return self._clock

    def _commit_writes(self, writes: List[tuple], by_transaction: bool = False) -> None:
        """Apply the writes in order, all or none: a failing write restores every document the commit touched
        (stored documents are replaced, never changed in place, so the old objects are the backup)."""
        stamp = self._tick()
        backup: Dict[Tuple[str, str], tuple] = {}
        for _, ref, _, _ in writes:
            col = ref.col
            backup.setdefault((col.name, ref.id), (col, col.docs.get(ref.id, _MISSING),
                                                   col.versions.get(ref.id, _MISSING), col.txn_versions.get(ref.id, _MISSING)))
        try:
            for kind, ref, data, extra in writes:
                ref._apply(kind, data, extra, by_transaction=by_transaction, stamp=stamp)
        except BaseException:
            for (_, doc_id), (col, doc, version, txn_version) in backup.items():
                for store, old in ((col.docs, doc), (col.versions, version), (col.txn_versions, txn_version)):
                    if old is _MISSING:
                        store.pop(doc_id, None)
                    else:
                        store[doc_id] = old
            raise

    def transaction(self) -> EvalTransaction:
        return EvalTransaction(self)

    def interfere_on_txn_read(self, collection: str, doc_id: str, fields: Dict[str, Any], after_read: bool = False) -> None:
        """Queue one update of `fields` (an `update`: dotted keys are paths) that another transaction commits when the
        next transactional `get` of the document happens (`EvalDocRef.get(transaction=)` and `get_all`; a query is not
        hooked: see `interfere_on_txn_query`). On a missing document the update raises `NotFound` from the reader's `get`. By default it lands before the snapshot is taken, so the
        reader sees the new data and `update_time`; with `after_read=True` it lands after, so the reader holds the old
        snapshot and its commit aborts and re-runs the body. Each call queues one conflict, consumed by one read."""
        self._interference.setdefault((collection, doc_id), []).append((fields, after_read))

    def interfere_on_txn_query(self, collection: str, doc_id: str, fields: Dict[str, Any]) -> None:
        """Queue one update of `fields` (an `update`: dotted keys are paths) that another transaction commits right after a
        transactional query returned the document, its read already noted: the reader holds the old snapshot, so its commit
        aborts and re-runs the body. A non-transactional query, or one that does not return the document, does not consume
        it. Each call queues one conflict, consumed by one query."""
        self._query_interference.setdefault((collection, doc_id), []).append(fields)

    def raise_on_txn_read(self, collection: str, doc_id: str, exc: BaseException) -> None:
        """Queue one error for the next transactional `get` of the document (a read-time `Aborted`, for instance)."""
        self._raises.setdefault((collection, doc_id), []).append(exc)

    def _raise_if_queued(self, ref: EvalDocRef) -> None:
        queued = self._raises.get((ref.col.name, ref.id))
        if queued:
            raise queued.pop(0)

    def _next_interference(self, ref: EvalDocRef) -> Optional[Tuple[Dict[str, Any], bool]]:
        queued = self._interference.get((ref.col.name, ref.id))
        return queued.pop(0) if queued else None

    def _interfere(self, ref: EvalDocRef, fields: Dict[str, Any]) -> None:
        self._commit_writes([("update", ref, fields, None)], by_transaction=True)

    async def get_all(self, refs: List[EvalDocRef], field_paths: Optional[List[str]] = None,   # type: ignore[no-untyped-def]
                      transaction: Any = None):
        for ref in reversed(refs):     # real Firestore promises no order; callers pair by `snap.id`
            yield await ref.get(transaction=transaction, field_paths=field_paths)

    def _check_index(self, name: str, is_group: bool, ops: List[tuple]) -> None:
        """`FailedPrecondition` naming the missing composite, when the index refusal is on and no declared index serves."""
        if self.declared is None:
            return
        group = name if is_group else self.groups.get(name, name.rsplit("/", 1)[-1])
        scope = "COLLECTION_GROUP" if is_group else "COLLECTION"
        required = _required_index(ops)
        if required is None:
            if is_group:    # automatic single-field indexes are COLLECTION scope only
                self._check_group_single_fields(group, ops)
            return
        head, tail = required
        if any(g == group and s == scope and _serves(fields, head, tail) for g, s, fields in self.declared):
            return
        wanted = sorted(f"{f} ({m})" for f, m in head) + [f"{f} ({m})" for f, m in tail]
        raise FailedPrecondition(
            f"The query requires an index: collection group {group!r}, scope {scope}, fields {wanted}")

    def _check_group_single_fields(self, group: str, ops: List[tuple]) -> None:
        """A collection-group query with a filter or ordering needs a COLLECTION_GROUP single-field index (a field
        override, or a one-field composite) on each field it uses; a bare stream needs none."""
        needed = {(op[1], "CONTAINS" if op[2] in _ARRAY_OPS else "FILTER") for op in ops if op[0] == "where"}
        needed |= {(op[1], op[2]) for op in ops if op[0] == "order_by"}   # an ordering needs its exact direction
        for field, kind in sorted(needed):
            wanted = {"CONTAINS": ("CONTAINS",), "FILTER": ("ASCENDING", "DESCENDING")}.get(kind, (kind,))
            if not any(g == group and s == "COLLECTION_GROUP" and len(fields) == 1 and fields[0][0] == field
                       and fields[0][1] in wanted for g, s, fields in self.declared or []):
                raise FailedPrecondition(
                    f"The query requires an index: collection group {group!r}, scope COLLECTION_GROUP, "
                    f"single field {field!r} ({kind})")
