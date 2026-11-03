"""Mutation survivors of the Firestore note store: `cleared()` filters by state and lists newest first; `delete_stub`
reads the stub inside its transaction."""
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.domain.drive_note import (
    DriveNote, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState, next_version,
    note_after_write,
)

from tests.unit.adapters.firestore_drive_eval_fakes import EvalSnap, eval_transactions
from tests.unit.adapters.firestore_drive_note_fakes import NOTES, NoteCollection, NoteDb, NoteDocRef

T = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
U = "u"
CHAT = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")


class _TxStrictDocRef(NoteDocRef):
    """A reference whose read is valid only inside a transaction: a plain `get()` here answers 'missing', so a
    transactional body that drops the `transaction=` argument sees no document."""

    async def get(self, transaction: Any = None, field_paths: Optional[list] = None) -> EvalSnap:
        if transaction is None and self.col.db.strict:   # type: ignore[attr-defined]
            return EvalSnap(self, None)
        return await super().get(transaction=transaction, field_paths=field_paths)


class _TxStrictCollection(NoteCollection):
    def document(self, doc_id: str) -> NoteDocRef:
        return _TxStrictDocRef(self, doc_id)


class _TxStrictDb(NoteDb):
    strict = False

    def collection(self, name: str) -> NoteCollection:   # type: ignore[override]
        if name not in self.cols:
            self.cols[name] = _TxStrictCollection(name, self)
        return self.cols[name]   # type: ignore[return-value]


def _repo(db: NoteDb) -> FirestoreDriveNoteRepository:
    env = MagicMock()
    env.drive_notes_collection = NOTES
    return FirestoreDriveNoteRepository(db, env)


@pytest.fixture
def db():
    with eval_transactions():
        yield NoteDb()


async def _put(repo, note_id: str, state: NoteState, at: datetime) -> None:
    version = next_version(0, NoteContent("the contractor's invoice"), NoteAction.SET, CHAT, at)
    note = note_after_write(DriveNote(note_id=note_id, user_id=U, account_id="acc"), version, state, CHAT, at,
                            vector=None, embedding_space="", embedding_pending=False,
                            binding=NoteBinding(item_id=note_id, identity=f"onedrive:d1:{note_id}", ref_tag="a1",
                                                bound_at=at),
                            snapshot=NoteSnapshot(name=f"{note_id}.pdf", path=f"Docs/{note_id}.pdf", snapshot_at=at))
    await repo.write_version(note, version, expected_version=0)


async def test_cleared_returns_only_cleared_notes_newest_first(db):
    repo = _repo(db)
    # updated_at is the store's own write clock: the write order is the age order (oldest first)
    await _put(repo, "c-old", NoteState.CLEARED, T)
    await _put(repo, "a-1", NoteState.ACTIVE, T)
    await _put(repo, "c-mid", NoteState.CLEARED, T)
    await _put(repo, "r-1", NoteState.ARCHIVED, T)
    await _put(repo, "c-new", NoteState.CLEARED, T)
    await _put(repo, "a-2", NoteState.ACTIVE, T)
    got = await repo.cleared(U, 10)
    assert [n.note_id for n in got] == ["c-new", "c-mid", "c-old"]


async def test_delete_stub_reads_the_stub_inside_its_transaction():
    db = _TxStrictDb()
    with eval_transactions():
        repo = _repo(db)
        await _put(repo, "n1", NoteState.ACTIVE, T)
        await repo.erase(U, ["n1"])
        db.strict = True
        assert await repo.delete_stub(U, "n1")
        db.strict = False
        assert await repo.get(U, "n1") is None
