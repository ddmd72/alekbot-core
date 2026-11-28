"""The day's items are capped per registered client inside the user's total, in the same transaction."""
from datetime import datetime, timezone

import pytest

from src.domain.memory_inbox import (
    RECEIVED_PER_CLIENT_PER_DAY,
    RECEIVED_PER_DAY,
    AddReceipt,
    Basis,
    SubmissionDraft,
    SubmittingClient,
    client_key,
)
from tests.support.memory_inbox_store import GROUPS, INDEXES_FILE, inbox_store

AT = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
DAY = "2026-11-25"
ONE = SubmittingClient(client_id="c1", client_name="Desk assistant")
TWO = SubmittingClient(client_id="cli-x", client_name="Other")


@pytest.fixture
def store(kit_db):
    return inbox_store(kit_db(indexes=INDEXES_FILE, groups=GROUPS))


def _draft() -> SubmissionDraft:
    return SubmissionDraft(information="Clara booked the Coimbra weekend", basis=Basis.SAID, said_when="",
                           happens_when="March 6-8")


def test_the_client_share_is_half_the_total():
    assert RECEIVED_PER_CLIENT_PER_DAY * 2 == RECEIVED_PER_DAY


async def test_one_client_stops_at_its_share_and_another_still_stores(store):
    first = await store.repo.add_submissions("u1", "a1", ONE, [_draft()] * 60, AT, DAY)
    assert first == AddReceipt(stored=RECEIVED_PER_CLIENT_PER_DAY, over_day=60 - RECEIVED_PER_CLIENT_PER_DAY)
    assert await store.repo.add_submissions("u1", "a1", ONE, [_draft()], AT, DAY) == AddReceipt(0, 1)
    assert await store.repo.add_submissions("u1", "a1", TWO, [_draft()] * 3, AT, DAY) == AddReceipt(3, 0)
    state = await store.repo.state("u1")
    assert state.received_day == (DAY, RECEIVED_PER_CLIENT_PER_DAY + 3)
    assert dict(state.received_by_client) == {client_key("c1"): RECEIVED_PER_CLIENT_PER_DAY, client_key("cli-x"): 3}


async def test_the_user_total_still_holds_across_clients(store):
    await store.repo.add_submissions("u1", "a1", ONE, [_draft()] * 50, AT, DAY)
    await store.repo.add_submissions("u1", "a1", TWO, [_draft()] * 50, AT, DAY)
    three = SubmittingClient(client_id="cli-b", client_name="Inspector")
    assert await store.repo.add_submissions("u1", "a1", three, [_draft()], AT, DAY) == AddReceipt(0, 1)


async def test_a_new_day_drops_the_clients_counts(store):
    await store.repo.add_submissions("u1", "a1", ONE, [_draft()] * 50, AT, DAY)
    assert await store.repo.add_submissions("u1", "a1", ONE, [_draft()] * 2, AT, "2026-11-26") == AddReceipt(2, 0)
    state = await store.repo.state("u1")
    assert state.received_day == ("2026-11-26", 2) and dict(state.received_by_client) == {client_key("c1"): 2}


async def test_a_failed_transaction_logs_the_exception_type_only(store, caplog, monkeypatch):
    monkeypatch.setattr(store.repo, "_the_state", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("Clara booked the Coimbra weekend")))
    with pytest.raises(RuntimeError):
        await store.repo.add_submissions("u1", "a1", ONE, [_draft()], AT, DAY)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "Coimbra" not in text
