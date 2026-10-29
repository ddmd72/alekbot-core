"""IndexedEmailRepository kit, the R1 methods (SECRETS_AT_REST_RFC §6.2, F1/F2): `set_reconnect_flags` and
`list_indexing_user_ids` of `FirestoreIndexedEmailRepository` on the evaluating Firestore fake, with the index refusal
ON. Assertions are on the raw stored documents and what the readers return."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexingState
from src.domain.reconnect_policy import Cleared
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)
MARK = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return FirestoreIndexedEmailRepository(db, env)


def states(repo):
    return repo.db.collection(STATE).docs


async def test_a_new_state_reads_unflagged_and_unmarked(repo):
    await repo.update_indexing_state(IndexingState(user_id="user-a", provider="gmail", indexed_through=D0))
    got = await repo.get_indexing_state("user-a", "gmail")
    assert (got.reconnect_notified, got.disconnected_by_user_at) == (False, None)


async def test_flags_are_written_field_by_field_and_read_back(repo):
    await repo.update_indexing_state(IndexingState(user_id="user-a", provider="gmail", indexed_through=D0))
    await repo.set_reconnect_flags("user-a", "gmail", reconnect_notified=True)
    await repo.set_reconnect_flags("user-a", "gmail", disconnected_by_user_at=MARK)
    stored = states(repo)["user-a_gmail"]
    assert stored["reconnect_notified"] is True and stored["disconnected_by_user_at"] == MARK
    assert stored["indexed_through"] == D0.replace(tzinfo=timezone.utc)   # the cursors are untouched
    got = await repo.get_indexing_state("user-a", "gmail")
    assert got.reconnect_notified is True
    assert got.disconnected_by_user_at == datetime(2026, 10, 29, 9, 30)   # naive UTC, as every date of this repo
    assert got.indexed_through == D0


async def test_none_leaves_a_flag_as_stored_and_cleared_empties_the_mark(repo):
    await repo.update_indexing_state(IndexingState(user_id="user-a", provider="gmail"))
    await repo.set_reconnect_flags("user-a", "gmail", reconnect_notified=True, disconnected_by_user_at=MARK)
    await repo.set_reconnect_flags("user-a", "gmail")                       # nothing given: nothing written
    assert states(repo)["user-a_gmail"]["reconnect_notified"] is True
    await repo.set_reconnect_flags("user-a", "gmail", reconnect_notified=False, disconnected_by_user_at=Cleared.MARK)
    got = await repo.get_indexing_state("user-a", "gmail")
    assert (got.reconnect_notified, got.disconnected_by_user_at) == (False, None)


async def test_a_cursor_update_never_resets_the_flags(repo):
    await repo.update_indexing_state(IndexingState(user_id="user-a", provider="gmail"))
    await repo.set_reconnect_flags("user-a", "gmail", reconnect_notified=True, disconnected_by_user_at=MARK)
    # the indexing job writes a state it built without the flags
    await repo.update_indexing_state(IndexingState(user_id="user-a", provider="gmail", indexed_through=D0))
    got = await repo.get_indexing_state("user-a", "gmail")
    assert got.reconnect_notified is True and got.disconnected_by_user_at is not None
    assert got.indexed_through == D0


async def test_a_missing_state_stays_missing(repo):
    await repo.set_reconnect_flags("user-a", "gmail", reconnect_notified=True, disconnected_by_user_at=MARK)
    assert states(repo) == {}
    assert await repo.get_indexing_state("user-a", "gmail") is None


async def test_list_indexing_user_ids_returns_the_providers_users_sorted(repo):
    for user, provider in (("user-b", "gmail"), ("user-a", "gmail"), ("user-c", "outlook")):
        await repo.update_indexing_state(IndexingState(user_id=user, provider=provider))
    assert await repo.list_indexing_user_ids("gmail") == ["user-a", "user-b"]
    assert await repo.list_indexing_user_ids("outlook") == ["user-c"]
    assert await repo.list_indexing_user_ids("other") == []
