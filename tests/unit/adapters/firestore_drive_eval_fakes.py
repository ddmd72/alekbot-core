"""An evaluating Firestore client for FirestoreDriveIndexRepository parity tests (USER_DRIVE_INDEXING_RFC §25.13.1).

The recording fakes (firestore_drive_fakes, firestore_drive_chunk_fakes) answer every query through a canned
`respond(ops)`; this one stores documents and evaluates the queries the repository sends — filters (`==`, null, ranges,
`in`, `array_contains`, `array_contains_any`), orderings with Firestore's value-type order and its implicit `__name__`
tie-break, `start_after` cursors shorter than the ordering, `limit`, `select` projections, cosine `find_nearest`,
`count`/`sum` aggregations, batches, `get_all`, merges with `Increment` and `DELETE_FIELD`, and transactions run
through a pass-through `async_transactional`. The same documents can then be loaded into the in-memory repository and
the two answers compared. Every executed query is logged in the recording fakes' op format, so the provisioning helpers
apply to it too. Not a Firestore emulator: only what the drive repository uses.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, InvalidArgument
from google.cloud import firestore
from google.cloud.firestore_v1.transforms import Increment
from google.cloud.firestore_v1.types import StructuredQuery
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository

RECS = "eval_drive_index_records"
STATES = "eval_drive_sync_state"
CHUNKS = "eval_drive_index_chunks"
HINTS = "eval_drive_index_hints"
USAGE = "eval_drive_model_usage"

_MISSING = object()
_DISJUNCTIONS_MAX = 30          # Firestore: values per `in` / `array_contains_any`, and the disjunction product
_ARRAY_OPS = ("array_contains", "array_contains_any")


def _check_shape(ops: List[tuple]) -> None:
    """Refuse what Firestore refuses with INVALID_ARGUMENT: a value list over 30, two array filters in one query, a
    disjunction product (`in` x `array_contains_any`) over 30, and a kNN whose pre-filter is not equality only."""
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
    if any(o[0] == "find_nearest" for o in ops) and any(op not in ("==", "IS_NULL") for _, _, op, _ in wheres):
        raise InvalidArgument("a vector query's pre-filters must be equality filters")


def _rank(value: Any) -> int:
    """Firestore's order of value types (null < bool < number < timestamp < string < ... < array < vector < map)."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return 2
    if isinstance(value, datetime):
        return 3
    if isinstance(value, str):
        return 4
    if isinstance(value, (list, tuple)):
        return 8
    if isinstance(value, Vector):
        return 9
    return 10


def _key(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return (8, tuple(_key(v) for v in value))
    return (_rank(value), value if _rank(value) in (1, 2, 3, 4) else 0)


class _Desc:
    """Inverts the order of a key, for DESCENDING orderings inside one tuple."""

    def __init__(self, key: Any) -> None:
        self.key = key

    def __lt__(self, other: "_Desc") -> bool:
        return other.key < self.key

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Desc) and other.key == self.key


def _get(data: Dict[str, Any], path: str) -> Any:
    return data.get(path, _MISSING)


def _match(data: Dict[str, Any], field: str, op: str, value: Any) -> bool:
    got = _get(data, field)
    if got is _MISSING:
        return False
    if op == "IS_NULL":
        return got is None
    if op == "==":
        return _rank(got) == _rank(value) and got == value
    if op in ("<", "<=", ">", ">="):
        if got is None or _rank(got) != _rank(value):
            return False
        return {"<": got < value, "<=": got <= value, ">": got > value, ">=": got >= value}[op]
    if op == "in":
        return any(_rank(got) == _rank(v) and got == v for v in value)
    if op == "array_contains":
        return isinstance(got, list) and value in got
    if op == "array_contains_any":
        return isinstance(got, list) and any(v in got for v in value)
    raise AssertionError(f"unsupported operator {op}")


def _resolve(value: Any, current: Any) -> Any:
    """A written value with its transforms applied over `current` (Increment adds, maps merge recursively)."""
    if isinstance(value, Increment):
        base = current if isinstance(current, (int, float)) and not isinstance(current, bool) else 0
        return base + value.value
    return copy.deepcopy(value)


def _merge(target: Dict[str, Any], data: Dict[str, Any]) -> None:
    for name, value in data.items():
        if value is firestore.DELETE_FIELD:
            target.pop(name, None)
        elif isinstance(value, dict):
            sub = target.get(name)
            if not isinstance(sub, dict):
                sub = {}
            _merge(sub, value)
            target[name] = sub
        else:
            target[name] = _resolve(value, target.get(name))


def _write(col: "EvalCollection", doc_id: str, data: Dict[str, Any], merge: Any = False) -> None:
    if merge is True:
        stored = copy.deepcopy(col.docs.get(doc_id, {}))
        _merge(stored, data)
    elif merge:
        stored = copy.deepcopy(col.docs.get(doc_id, {}))
        for name in merge:
            value = data.get(name, firestore.DELETE_FIELD)
            if value is firestore.DELETE_FIELD:
                stored.pop(name, None)
            else:
                stored[name] = _resolve(value, stored.get(name))
    else:
        stored = {}
        _merge(stored, data)
    col.docs[doc_id] = stored


class EvalSnap:
    def __init__(self, ref: "EvalDocRef", data: Optional[Dict[str, Any]]) -> None:
        self.reference = ref
        self.id = ref.id
        self.exists = data is not None
        self._data = data

    def to_dict(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self._data) if self._data is not None else None


def _project(data: Dict[str, Any], paths: Optional[List[str]]) -> Dict[str, Any]:
    if paths is None:
        return copy.deepcopy(data)
    return {p: copy.deepcopy(data[p]) for p in paths if p in data}


class EvalDocRef:
    def __init__(self, col: "EvalCollection", doc_id: str) -> None:
        self.col, self.id = col, doc_id

    @property
    def doc_id(self) -> str:
        return self.id

    async def get(self, transaction: Any = None, field_paths: Optional[List[str]] = None) -> EvalSnap:
        data = self.col.docs.get(self.id)
        return EvalSnap(self, None if data is None else _project(data, field_paths))

    async def set(self, data: Dict[str, Any], merge: Any = False) -> None:
        _write(self.col, self.id, data, merge)

    async def update(self, data: Dict[str, Any]) -> None:
        assert self.id in self.col.docs, "update of a missing document"
        _write(self.col, self.id, data, merge=list(data))

    async def create(self, data: Dict[str, Any]) -> None:
        """Firestore's create: AlreadyExists when the document is there, nothing written."""
        if self.id in self.col.docs:
            raise AlreadyExists(f"{self.col.name}/{self.id}")
        _write(self.col, self.id, data, False)

    async def delete(self) -> None:
        self.col.docs.pop(self.id, None)


class EvalQuery:
    def __init__(self, col: "EvalCollection", ops: List[tuple]) -> None:
        self.col, self.ops = col, ops

    def _then(self, op: tuple) -> "EvalQuery":
        return EvalQuery(self.col, self.ops + [op])

    def where(self, *, filter: Any) -> "EvalQuery":
        op = filter.op_string
        op = op if isinstance(op, str) else StructuredQuery.UnaryFilter.Operator(op).name
        return self._then(("where", filter.field_path, op, filter.value))

    def order_by(self, field: str, direction: str = "ASCENDING") -> "EvalQuery":
        return self._then(("order_by", field, direction))

    def limit(self, n: int) -> "EvalQuery":
        return self._then(("limit", n))

    def select(self, paths: Any) -> "EvalQuery":
        return self._then(("select", tuple(str(p) for p in paths)))

    def start_after(self, values: Dict[str, Any]) -> "EvalQuery":
        return self._then(("start_after", dict(values)))

    def find_nearest(self, **kw: Any) -> "EvalQuery":
        return self._then(("find_nearest", kw))

    def count(self, alias: Optional[str] = None) -> "EvalQuery":
        return self._then(("count", alias))

    def sum(self, field: str, alias: Optional[str] = None) -> "EvalQuery":
        return self._then(("sum", field, alias))

    async def get(self) -> List[Any]:
        self.col.executed.append(self.ops)
        _check_shape(self.ops)
        if self.col.fail is not None:
            raise self.col.fail
        return self.col.evaluate(self.ops)

    async def stream(self):   # type: ignore[no-untyped-def]
        for s in await self.get():
            yield s


class EvalCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: Dict[str, Dict[str, Any]] = {}
        self.executed: List[List[tuple]] = []
        self.fail: Optional[Exception] = None

    def where(self, *, filter: Any) -> EvalQuery:
        return EvalQuery(self, []).where(filter=filter)

    def select(self, paths: Any) -> EvalQuery:
        return EvalQuery(self, []).select(paths)

    def order_by(self, field: str, direction: str = "ASCENDING") -> EvalQuery:
        return EvalQuery(self, []).order_by(field, direction)

    def document(self, doc_id: str) -> EvalDocRef:
        return EvalDocRef(self, doc_id)

    def evaluate(self, ops: List[tuple]) -> List[Any]:
        rows = [(doc_id, data) for doc_id, data in sorted(self.docs.items())]
        orders: List[tuple] = []
        cursor: Optional[Dict[str, Any]] = None
        limit: Optional[int] = None
        paths: Optional[List[str]] = None
        nearest: Optional[Dict[str, Any]] = None
        aggregate: Optional[tuple] = None
        for op in ops:
            if op[0] == "where":
                rows = [(i, d) for i, d in rows if _match(d, op[1], op[2], op[3])]
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
        for name, _ in orders:   # an ordering excludes documents without the field
            rows = [(i, d) for i, d in rows if name in d]
        last = orders[-1][1] if orders else "ASCENDING"

        def sort_key(row: tuple) -> tuple:
            doc_id, data = row
            parts = [(_Desc(_key(data[n])) if direction == "DESCENDING" else _key(data[n])) for n, direction in orders]
            parts.append(_Desc(doc_id) if last == "DESCENDING" else doc_id)   # implicit __name__
            return tuple(parts)

        rows.sort(key=sort_key)
        if cursor is not None:
            names = list(cursor)
            assert names == [n for n, _ in orders][:len(names)], (names, orders)

            def prefix(data: Dict[str, Any]) -> tuple:
                return tuple(_Desc(_key(data[n])) if d == "DESCENDING" else _key(data[n])
                             for n, d in orders[:len(names)])

            at = tuple(_Desc(_key(cursor[n])) if d == "DESCENDING" else _key(cursor[n])
                       for n, d in orders[:len(names)])
            rows = [(i, d) for i, d in rows if at < prefix(d)]
        if aggregate is not None:
            if aggregate[0] == "count":
                return [[SimpleNamespace(alias=aggregate[1], value=len(rows[:limit] if limit else rows))]]
            total = sum(d.get(aggregate[1]) or 0 for _, d in rows)
            return [[SimpleNamespace(alias=aggregate[2], value=total)]]
        if limit is not None:
            rows = rows[:limit]
        return [EvalSnap(self.document(i), _project(d, paths)) for i, d in rows]

    def _nearest(self, rows: List[tuple], spec: Dict[str, Any], paths: Optional[List[str]]) -> List[EvalSnap]:
        field, query = spec["vector_field"], list(spec["query_vector"])
        scored = []
        for doc_id, data in rows:
            vector = data.get(field)
            if not isinstance(vector, Vector):
                continue
            v = list(vector)
            dot = sum(a * b for a, b in zip(v, query))
            na, nb = math.sqrt(sum(a * a for a in v)), math.sqrt(sum(b * b for b in query))
            scored.append((1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb), doc_id, data))
        scored.sort(key=lambda t: (t[0], t[1]))
        out = []
        for distance, doc_id, data in scored[:spec["limit"]]:
            shown = {**data, spec["distance_result_field"]: distance}
            out.append(EvalSnap(self.document(doc_id), _project(shown, paths)))
        return out


class EvalBatch:
    def __init__(self, db: "EvalDb") -> None:
        self.db = db
        self.writes: List[tuple] = []

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self.writes.append(("set", ref, data, merge))

    def update(self, ref: EvalDocRef, data: Dict[str, Any]) -> None:
        self.writes.append(("update", ref, data, list(data)))

    def delete(self, ref: EvalDocRef) -> None:
        self.writes.append(("delete", ref, None, None))

    async def commit(self) -> None:
        assert len(self.writes) <= 500, "Firestore takes at most 500 writes per batch"
        self.db.commits.append(list(self.writes))
        for kind, ref, data, merge in self.writes:
            if kind == "delete":
                ref.col.docs.pop(ref.id, None)
            else:
                _write(ref.col, ref.id, data, merge)


class EvalTransaction:
    """Writes apply at once; the pass-through `async_transactional` runs the function a single time."""

    def __init__(self, db: "EvalDb") -> None:
        self.db = db
        self.writes: List[tuple] = []

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self.writes.append(("set", ref.id))
        _write(ref.col, ref.id, data, merge)

    def update(self, ref: EvalDocRef, data: Dict[str, Any]) -> None:
        self.writes.append(("update", ref.id))
        _write(ref.col, ref.id, data, merge=list(data))

    def delete(self, ref: EvalDocRef) -> None:
        self.writes.append(("delete", ref.id))
        ref.col.docs.pop(ref.id, None)


class EvalDb:
    def __init__(self) -> None:
        self.cols: Dict[str, EvalCollection] = {}
        self.commits: List[List[tuple]] = []
        self.transactions: List[EvalTransaction] = []
        self.get_all_calls: List[tuple] = []

    def collection(self, name: str) -> EvalCollection:
        if name not in self.cols:
            self.cols[name] = EvalCollection(name)
        return self.cols[name]

    def batch(self) -> EvalBatch:
        return EvalBatch(self)

    def transaction(self) -> EvalTransaction:
        tx = EvalTransaction(self)
        self.transactions.append(tx)
        return tx

    async def get_all(self, refs: List[EvalDocRef], field_paths: Optional[List[str]] = None):   # type: ignore[no-untyped-def]
        self.get_all_calls.append(([r.id for r in refs], field_paths))
        for ref in refs:
            yield await ref.get(field_paths=field_paths)


def eval_transactions():
    """Patch the repository's `async_transactional` into a plain call (the evaluating transaction applies at once)."""
    return patch("src.adapters.firestore_drive_index_repo.firestore.async_transactional", side_effect=lambda fn: fn)


def make_eval_repo(db: EvalDb, env_config: Any) -> FirestoreDriveIndexRepository:
    env_config.drive_index_records_collection = RECS
    env_config.drive_sync_state_collection = STATES
    env_config.drive_index_chunks_collection = CHUNKS
    env_config.drive_index_hints_collection = HINTS
    env_config.drive_model_usage_collection = USAGE
    return FirestoreDriveIndexRepository(db, env_config)
