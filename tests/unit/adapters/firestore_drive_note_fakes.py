"""The evaluating Firestore client (firestore_drive_eval_fakes) with what the drive-note store also uses: subcollections
(`doc_ref.collection("versions")`, kept as their own collection named `<parent collection>/<doc id>/<name>`) and
`SERVER_TIMESTAMP` resolved to a strictly increasing clock when a transaction or a document reference writes it. Queries,
cursors, projections, kNN, counts, batches and the pass-through transactions are the shared fake's."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from google.cloud import firestore

from tests.unit.adapters.firestore_drive_eval_fakes import EvalCollection, EvalDb, EvalDocRef, EvalTransaction

NOTES = "eval_drive_notes"
T0 = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def tick(self) -> datetime:
        self.now = self.now + timedelta(milliseconds=1)
        return self.now


def _stamped(data: Dict[str, Any], clock: _Clock) -> Dict[str, Any]:
    return {k: (clock.tick() if v is firestore.SERVER_TIMESTAMP else v) for k, v in data.items()}


class NoteDocRef(EvalDocRef):
    def collection(self, name: str) -> EvalCollection:
        db: NoteDb = self.col.db   # type: ignore[attr-defined]
        return db.collection(f"{self.col.name}/{self.id}/{name}")

    async def set(self, data: Dict[str, Any], merge: Any = False) -> None:
        await super().set(_stamped(data, self.col.db.clock), merge)   # type: ignore[attr-defined]

    async def update(self, data: Dict[str, Any]) -> None:
        await super().update(_stamped(data, self.col.db.clock))   # type: ignore[attr-defined]


class NoteCollection(EvalCollection):
    def __init__(self, name: str, db: "NoteDb") -> None:
        super().__init__(name)
        self.db = db

    def document(self, doc_id: str) -> NoteDocRef:
        return NoteDocRef(self, doc_id)


class NoteTransaction(EvalTransaction):
    """Records each write's data as the adapter sent it (`raw`, sentinels included) before stamping it."""

    def __init__(self, db: "NoteDb") -> None:
        super().__init__(db)
        self.raw: List[tuple] = []

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self.raw.append(("set", ref.id, dict(data)))
        super().set(ref, _stamped(data, self.db.clock), merge)   # type: ignore[attr-defined]

    def update(self, ref: EvalDocRef, data: Dict[str, Any]) -> None:
        self.raw.append(("update", ref.id, dict(data)))
        super().update(ref, _stamped(data, self.db.clock))   # type: ignore[attr-defined]


class NoteDb(EvalDb):
    def __init__(self) -> None:
        super().__init__()
        self.clock = _Clock()

    def collection(self, name: str) -> NoteCollection:   # type: ignore[override]
        if name not in self.cols:
            self.cols[name] = NoteCollection(name, self)
        return self.cols[name]   # type: ignore[return-value]

    def transaction(self) -> NoteTransaction:
        tx = NoteTransaction(self)
        self.transactions.append(tx)
        return tx

    def versions_of(self, doc_id: str) -> Dict[str, Dict[str, Any]]:
        col = self.cols.get(f"{NOTES}/{doc_id}/versions")
        return dict(col.docs) if col is not None else {}

    def executed(self) -> List[List[tuple]]:
        return self.collection(NOTES).executed


class BufferedTransaction(NoteTransaction):
    """A transaction whose writes apply only at `commit()`, as Firestore's do: a run that is retried writes nothing."""

    def __init__(self, db: "NoteDb") -> None:
        super().__init__(db)
        self.pending: List[tuple] = []

    def set(self, ref: EvalDocRef, data: Dict[str, Any], merge: Any = False) -> None:
        self.pending.append(("set", ref, data, merge))

    def update(self, ref: EvalDocRef, data: Dict[str, Any]) -> None:
        self.pending.append(("update", ref, data, None))

    def delete(self, ref: EvalDocRef) -> None:
        self.pending.append(("delete", ref, None, None))

    def commit(self) -> None:
        for kind, ref, data, merge in self.pending:
            if kind == "set":
                NoteTransaction.set(self, ref, data, merge)
            elif kind == "update":
                NoteTransaction.update(self, ref, data)
            else:
                EvalTransaction.delete(self, ref)


def contended(db: "NoteDb", interloper: Any) -> Any:
    """A stand-in for `firestore.async_transactional` with one contention: the first run reads, then `interloper()`
    commits another writer's change, so that run's commit aborts (its buffered writes are dropped) and the function runs
    again on fresh reads, as the real client's retry does. Every later run commits."""
    def decorator(fn: Any) -> Any:
        async def run(_transaction: Any) -> Any:
            runs = 0
            while True:
                tx = BufferedTransaction(db)
                result = await fn(tx)
                runs += 1
                if runs == 1:
                    await interloper()
                    continue
                tx.commit()
                return result
        return run
    return decorator
