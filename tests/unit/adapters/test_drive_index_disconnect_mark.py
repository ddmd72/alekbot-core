"""R1's deliberate-disconnect mark on the drive sync state (SECRETS_AT_REST_RFC §6.2, F1): a fan-out flag like the
others. `update_sync_flags(disconnected_by_user_at=...)` writes it on both adapters (Firestore over the evaluating fake),
`Cleared.MARK` empties it, `save_sync_state` never touches it, a missing state stays missing, and the tick's document
does not carry it."""
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_drive_index_repo import state_from_doc, state_to_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveSyncState
from src.domain.reconnect_policy import Cleared

from tests.unit.adapters.firestore_drive_eval_fakes import STATES, EvalDb, eval_transactions, make_eval_repo

T = datetime(2026, 10, 1, tzinfo=timezone.utc)
MARK = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
A = ConnectionScope(user_id="user-a", connection_id="user-a")


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repos(db, mock_env_config):
    with eval_transactions():
        yield InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)


async def test_the_mark_is_written_kept_by_a_save_and_cleared(repos, db):
    for repo in repos:
        await repo.save_sync_state(DriveSyncState(user_id="user-a", connection_id="user-a", created_at=T))
        await repo.update_sync_flags(A, disconnected_by_user_at=MARK)
        assert (await repo.get_sync_state(A)).disconnected_by_user_at == MARK
        # the tick's save carries a state read before the mark: the mark stays as stored
        await repo.save_sync_state(DriveSyncState(user_id="user-a", connection_id="user-a", created_at=T, epoch=2))
        got = await repo.get_sync_state(A)
        assert got.epoch == 2 and got.disconnected_by_user_at == MARK
        # None leaves it; another flag's update leaves it too
        await repo.update_sync_flags(A, reconnect_notified=True)
        assert (await repo.get_sync_state(A)).disconnected_by_user_at == MARK
        await repo.update_sync_flags(A, disconnected_by_user_at=Cleared.MARK)
        got = await repo.get_sync_state(A)
        assert got.disconnected_by_user_at is None and got.reconnect_notified is True
    assert db.cols[STATES].docs["user-a"]["disconnected_by_user_at"] is None


async def test_a_missing_state_stays_missing():
    """In memory only: the drive eval fake asserts on an update of a missing document instead of raising NotFound, so
    the Firestore adapter's `except NotFound` is not reachable there."""
    repo = InMemoryDriveIndexRepository()
    await repo.update_sync_flags(A, disconnected_by_user_at=MARK)
    assert await repo.get_sync_state(A) is None


def test_the_mark_is_not_in_the_ticks_document_and_reads_back_from_a_stored_one():
    doc = state_to_doc(DriveSyncState(user_id="user-a", connection_id="user-a", disconnected_by_user_at=MARK))
    assert "disconnected_by_user_at" not in doc
    assert state_from_doc("user-a", {**doc, "disconnected_by_user_at": MARK}).disconnected_by_user_at == MARK
    assert state_from_doc("user-a", doc).disconnected_by_user_at is None
