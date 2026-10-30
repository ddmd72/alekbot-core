"""Erase on the Firestore note store: a store error on one note is reported in `EraseResult.left`, never raised, and
logged at the level the store's other methods give the same error: a missing or building index (FailedPrecondition)
at ERROR, a transient error at WARNING (core re-review 2, carried to the plan's Task 0)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository
from src.domain.drive_note import (
    DriveNote, EraseResult, NoteAction, NoteBinding, NoteContent, NoteProvenance, NoteSnapshot, NoteState,
    next_version, note_after_write,
)

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_note_fakes import NOTES, NoteDb

T = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
U = "u"
CHAT = NoteProvenance(origin="chat", session_id="s1", turn_id="t1")


@pytest.fixture
def repo():
    env = MagicMock()
    env.drive_notes_collection = NOTES
    with eval_transactions():
        yield FirestoreDriveNoteRepository(NoteDb(), env)


async def _first(repo, note_id: str, item: str) -> None:
    version = next_version(0, NoteContent("the contractor's invoice"), NoteAction.SET, CHAT, T)
    note = note_after_write(DriveNote(note_id=note_id, user_id=U, account_id="acc"), version, NoteState.ACTIVE, CHAT, T,
                            vector=None, embedding_space="", embedding_pending=False,
                            binding=NoteBinding(item_id=item, identity=f"onedrive:d1:{item}", ref_tag="a1", bound_at=T),
                            snapshot=NoteSnapshot(name=f"{item}.pdf", path=f"Docs/{item}.pdf", snapshot_at=T))
    await repo.write_version(note, version, expected_version=0)


def _failing_on(repo, note_id: str, exc: Exception):
    real = repo._erase_one

    async def erase_one(user_id, nid):
        if nid == note_id:
            raise exc
        return await real(user_id, nid)

    return erase_one


async def test_a_missing_index_on_one_note_is_reported_left_and_logged_at_error(repo):
    await _first(repo, "n1", "a")
    await _first(repo, "n2", "b")
    repo._erase_one = _failing_on(repo, "n1", FailedPrecondition("the query requires an index"))
    with patch("src.adapters.firestore_drive_note_repo.logger") as log:
        result = await repo.erase(U, ["n1", "n2"])
    assert result == EraseResult(erased=1, left=("n1",))
    assert (await repo.get(U, "n2")).erase_pending
    assert not (await repo.get(U, "n1")).erase_pending
    [line] = [c.args[0] for c in log.error.call_args_list]
    assert "n1" in line and "index" in line and "FailedPrecondition" in line
    assert not any("n1" in c.args[0] for c in log.warning.call_args_list)


async def test_a_transient_error_on_one_note_stays_a_warning(repo):
    await _first(repo, "n1", "a")
    repo._erase_one = _failing_on(repo, "n1", ServiceUnavailable("down"))
    with patch("src.adapters.firestore_drive_note_repo.logger") as log:
        result = await repo.erase(U, ["n1"])
    assert result == EraseResult(erased=0, left=("n1",))
    assert log.error.call_count == 0
    assert any("n1" in c.args[0] and "ServiceUnavailable" in c.args[0] for c in log.warning.call_args_list)
