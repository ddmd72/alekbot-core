"""The note store's transactional reads really run inside their transaction: a document another transaction changes
between the read and the commit makes the body re-run (and see the new data), and a read-time error reaches the caller.
A read made outside the transaction (the `transaction=` keyword dropped) would not be protected."""
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.domain.drive_note import (
    DriveNote, DriveNoteConflict, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState,
    next_version, note_after_write,
)

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_note_fakes import NOTES, NoteCollection, NoteDb, NoteDocRef

T = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
U = "user-1"
CHAT = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")
DOC = f"{U}:n1"
VERSIONS = f"{NOTES}/{DOC}/versions"


class _TxnDocRef(NoteDocRef):
    """A read made WITH a transaction meets what another transaction does meanwhile: a queued concurrent change lands
    (the body's re-run reads it) or a queued read-time error is raised. A read without a transaction is untouched."""

    async def get(self, transaction=None, field_paths=None):   # type: ignore[no-untyped-def]
        if transaction is not None:
            db = self.col.db
            key = (self.col.name, self.id)
            if db.errors.get(key):
                raise db.errors[key].pop(0)
            if db.changes.get(key):
                await NoteDocRef.update(self, db.changes[key].pop(0))
        return await super().get(transaction=transaction, field_paths=field_paths)


class _TxnCollection(NoteCollection):
    def document(self, doc_id):   # type: ignore[no-untyped-def]
        return _TxnDocRef(self, doc_id)


class _TxnDb(NoteDb):
    def __init__(self) -> None:
        super().__init__()
        self.errors: dict = {}
        self.changes: dict = {}

    def collection(self, name):   # type: ignore[no-untyped-def]
        if name not in self.cols:
            self.cols[name] = _TxnCollection(name, self)
        return self.cols[name]

    def interfere_on_txn_read(self, collection, doc_id, fields, after_read=False):   # type: ignore[no-untyped-def]
        self.changes.setdefault((collection, doc_id), []).append(fields)

    def raise_on_txn_read(self, collection, doc_id, exc):   # type: ignore[no-untyped-def]
        self.errors.setdefault((collection, doc_id), []).append(exc)


@pytest.fixture
def db():
    return _TxnDb()


@pytest.fixture
def repo(db):
    env = MagicMock()
    env.drive_notes_collection = NOTES
    with eval_transactions():
        yield FirestoreDriveNoteRepository(db, env)


def _write_args(previous: Optional[DriveNote], text: str):
    n = previous.current_version if previous else 0
    version = next_version(n, NoteContent(text), NoteAction.SET, CHAT, T)
    note = note_after_write(previous or DriveNote(note_id="n1", user_id=U, account_id="acct-1"), version,
                            NoteState.ACTIVE, CHAT, T, vector=None, embedding_space="", embedding_pending=False,
                            binding=NoteBinding(item_id="a", identity="onedrive:d1:a", ref_tag="a1", bound_at=T),
                            snapshot=NoteSnapshot(name="a.pdf", path="Docs/a.pdf", snapshot_at=T))
    return note, version, n


async def _first(repo) -> DriveNote:
    note, version, n = _write_args(None, "first")
    await repo.write_version(note, version, expected_version=n)
    return await repo.get(U, "n1")


async def test_write_version_reads_the_parent_inside_its_transaction(repo, db):
    stored = await _first(repo)
    note, version, n = _write_args(stored, "second")
    db.interfere_on_txn_read(NOTES, DOC, {"revision": 99})
    with pytest.raises(DriveNoteConflict, match="revision"):
        await repo.write_version(note, version, expected_version=n)
    assert (await repo.get(U, "n1")).current_version == 1


async def test_write_version_reads_the_closing_version_inside_its_transaction(repo, db):
    stored = await _first(repo)
    note, version, n = _write_args(stored, "second")
    db.raise_on_txn_read(VERSIONS, "v1", RuntimeError("read-time error"))
    with pytest.raises(RuntimeError, match="read-time error"):
        await repo.write_version(note, version, expected_version=n)


async def test_a_lifecycle_update_re_reads_the_parent_after_a_concurrent_commit(repo, db):
    await _first(repo)
    db.interfere_on_txn_read(NOTES, DOC, {"revision": 50})
    snap = NoteSnapshot(name="b.pdf", path="Docs/b.pdf", snapshot_at=T)
    assert await repo.refresh_snapshot(U, "n1", snap) is True
    after = await repo.get(U, "n1")
    assert after.revision == 51 and after.snapshot.name == "b.pdf"


async def test_erase_reads_the_parent_inside_the_stub_transaction(repo, db):
    await _first(repo)
    db.interfere_on_txn_read(NOTES, DOC, {"revision": 50})
    result = await repo.erase(U, ["n1"])
    assert result.erased == 1
    assert (await repo.get(U, "n1")).revision == 51
