"""Additions to the evaluating Firestore fake (firestore_drive_eval_fakes) for the transcript documents: `create`
(AlreadyExists on a present document), an `update` of a missing document raising NotFound as the SDK does, a document
`path` and `EvalDb.collection_group` (the purge's orphan sweep; its executed queries are logged in
`db.group_executed[group]`). Patched in from here so the shared fake stays as it is."""
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, NotFound

from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb, EvalDocRef, EvalQuery, _write


async def _create(self: EvalDocRef, data: Dict[str, Any]) -> None:
    if self.id in self.col.docs:
        raise AlreadyExists(self.id)
    _write(self.col, self.id, data)


_plain_update = EvalDocRef.update


async def _update(self: EvalDocRef, data: Dict[str, Any]) -> None:
    if self.id not in self.col.docs:
        raise NotFound(self.id)
    await _plain_update(self, data)


def _path(self: EvalDocRef) -> str:
    return f"{self.col.name}/{self.id}"


class _GroupQuery:
    """Every collection whose name ends in `/<group>`, queried with one op list; the limit applies to the union."""

    def __init__(self, db: EvalDb, group: str, ops: List[tuple]) -> None:
        self.db, self.group, self.ops = db, group, ops

    def _then(self, op: tuple) -> "_GroupQuery":
        return _GroupQuery(self.db, self.group, self.ops + [op])

    def where(self, *, filter: Any) -> "_GroupQuery":
        return _GroupQuery(self.db, self.group, EvalQuery(None, self.ops).where(filter=filter).ops)   # type: ignore[arg-type]

    def order_by(self, field: str, direction: str = "ASCENDING") -> "_GroupQuery":
        return self._then(("order_by", field, direction))

    def select(self, paths: Any) -> "_GroupQuery":
        return self._then(("select", list(paths)))

    def limit(self, n: int) -> "_GroupQuery":
        return self._then(("limit", n))

    async def get(self) -> List[Any]:
        self.db.group_executed.setdefault(self.group, []).append(self.ops)
        rows: List[Any] = []
        for name, col in list(self.db.cols.items()):
            if name.endswith(f"/{self.group}"):
                rows += col.evaluate(self.ops)
        limits = [op[1] for op in self.ops if op[0] == "limit"]
        return rows[:limits[-1]] if limits else rows


def _collection_group(self: EvalDb, group: str) -> _GroupQuery:
    if not hasattr(self, "group_executed"):
        self.group_executed = {}
    return _GroupQuery(self, group, [])


@contextmanager
def transcript_doc_fakes() -> Iterator[None]:
    with patch.object(EvalDocRef, "create", _create, create=True), patch.object(EvalDocRef, "update", _update), \
            patch.object(EvalDocRef, "path", property(_path), create=True), \
            patch.object(EvalDb, "collection_group", _collection_group, create=True):
        yield
