"""The janitor's reads and its one write (Facts S3 · T3): `find_due_facts`, `stale_due_fact`, `stale_backlog`, on the
evaluating Firestore fake with the index refusal ON (`config/firestore.indexes.json`), so a query no declared index serves
fails here. Every document is seeded through a real write, so each carries its own change token.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from google.cloud.firestore_v1.vector import Vector

from src.domain.entities import ExpiryKind, FactState
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_lifecycle_sweep import stale_fields
from tests.support.fact_kit import ACC, DROP, OTHER, T0, make_fact, put_raw
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=1)
FUTURE = NOW + timedelta(days=1)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def seed(store, text, **over):
    fact = make_fact(text, **over)
    await store.repo.add_fact(fact)
    return fact


async def due_ids(store, now=NOW):
    return sorted(d.fact_id for d in await store.repo.find_due_facts(now))


# ------------------------------------------------------------------------------------------------ find_due_facts

async def test_due_facts_are_current_facts_whose_expires_at_has_passed_in_every_account(store):
    await seed(store, "due-a", expires_at=PAST, expiry_kind=ExpiryKind.HORIZON, ttl_days=30)
    await seed(store, "due-b", account=OTHER, expires_at=NOW, expiry_kind=ExpiryKind.DATE)
    await seed(store, "later", expires_at=FUTURE, expiry_kind=ExpiryKind.DATE)
    assert await due_ids(store) == ["f-due-a", "f-due-b"]


async def test_due_facts_leave_out_every_other_state(store):
    for state in (FactState.STALE, FactState.ARCHIVED, FactState.INVALIDATED, FactState.SUPERSEDED):
        await seed(store, f"gone-{state.value}", expires_at=PAST, expiry_kind=ExpiryKind.DATE, state=state)
    await seed(store, "live", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    assert await due_ids(store) == ["f-live"]


async def test_a_fact_without_expires_at_is_never_due(store):
    await seed(store, "no-date", expiry_kind=ExpiryKind.NONE)
    put_raw(store, "missing", "missing", expires_at=DROP)
    put_raw(store, "text-date", "text-date", expires_at="2020-01-01")
    assert await due_ids(store, now=datetime(2100, 1, 1, tzinfo=timezone.utc)) == []


async def test_a_due_document_without_expiry_kind_is_returned_with_none_so_the_service_can_skip_it(store):
    await seed(store, "old-writer", expires_at=PAST)
    put_raw(store, "dropped", "dropped", entity={"expires_at": PAST}, expiry_kind=DROP)
    got = {d.fact_id: d for d in await store.repo.find_due_facts(NOW)}
    assert set(got) == {"f-old-writer", "dropped"}
    assert got["f-old-writer"].expiry_kind is None and got["dropped"].expiry_kind is None


async def test_a_due_fact_is_a_projection_without_text_or_vectors(store):
    fact = await seed(store, "secret text", expires_at=PAST, expiry_kind=ExpiryKind.HORIZON, ttl_days=7)
    (due,) = await store.repo.find_due_facts(NOW)
    assert (due.fact_id, due.account_id, due.expires_at, due.expiry_kind) == (fact.id, ACC, PAST, ExpiryKind.HORIZON)
    assert due.lifecycle_events == []
    assert not hasattr(due, "text") and not hasattr(due, "vector")


async def test_a_due_fact_carries_its_trail(store):
    event = {"at": T0, "from": None, "to": "current", "actor": "stage1", "reason": "version 2 of x"}
    await seed(store, "traced", expires_at=PAST, expiry_kind=ExpiryKind.DATE, lifecycle_events=[event])
    (due,) = await store.repo.find_due_facts(NOW)
    assert due.lifecycle_events == [event]


async def test_a_legacy_due_document_without_account_id_is_skipped(store):
    put_raw(store, "legacy", "legacy", expires_at=PAST, expiry_kind="date", account_id=DROP, created_by_user_id=DROP,
            owner_id=ACC)
    await seed(store, "fine", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    assert await due_ids(store) == ["f-fine"]


async def test_the_due_query_filters_the_stored_expires_at_field(store):
    await seed(store, "due", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    store.docs["f-due"]["expiry_deadline"] = store.docs["f-due"].pop("expires_at")
    assert await due_ids(store) == []


# ----------------------------------------------------------------------------------------------- stale_due_fact

async def test_staling_changes_the_lifecycle_fields_and_the_trail_only(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.HORIZON, ttl_days=30)
    (due,) = await store.repo.find_due_facts(NOW)
    before = copy.deepcopy(store.docs["f-x"])
    await store.repo.stale_due_fact(due, stale_fields(due, NOW))
    after = store.docs["f-x"]
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"state", "stale_since", "last_updated", "lifecycle_events"}
    assert after["state"] == "stale" and type(after["state"]) is str
    assert after["stale_since"] == NOW and after["last_updated"] == NOW
    assert after["is_current"] is True and after["valid_to"] is None
    assert after["expires_at"] == PAST and after["expiry_kind"] == "horizon"
    assert isinstance(after["vector"], Vector) and after["text"] == "x"


async def test_staling_appends_an_expired_event_to_the_existing_trail(store):
    earlier = {"at": T0, "from": None, "to": "current", "actor": "stage1", "reason": "version 2 of x"}
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE, lifecycle_events=[earlier])
    (due,) = await store.repo.find_due_facts(NOW)
    await store.repo.stale_due_fact(due, stale_fields(due, NOW))
    assert store.docs["f-x"]["lifecycle_events"] == [
        earlier,
        {"at": NOW, "from": "current", "to": "stale", "actor": "janitor", "reason": f"expired {PAST.isoformat()}"},
    ]


async def test_a_staled_fact_leaves_the_due_query_and_enters_the_backlog(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    (due,) = await store.repo.find_due_facts(NOW)
    await store.repo.stale_due_fact(due, stale_fields(due, NOW))
    assert await due_ids(store) == []
    assert await store.repo.stale_backlog() == {ACC: (1, NOW)}


async def test_staling_a_fact_that_changed_since_it_was_read_conflicts_and_writes_nothing(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    (due,) = await store.repo.find_due_facts(NOW)
    await store.db.collection(FACTS).document("f-x").update({"expires_at": FUTURE})
    before = copy.deepcopy(store.docs)
    with pytest.raises(FactWriteConflict):
        await store.repo.stale_due_fact(due, stale_fields(due, NOW))
    assert store.docs == before


async def test_staling_a_deleted_fact_conflicts(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    (due,) = await store.repo.find_due_facts(NOW)
    await store.db.collection(FACTS).document("f-x").delete()
    with pytest.raises(FactWriteConflict):
        await store.repo.stale_due_fact(due, stale_fields(due, NOW))


async def test_a_conflict_during_the_commit_is_a_write_conflict(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    (due,) = await store.repo.find_due_facts(NOW)
    store.interfere("f-x", {"tags": ["other"]})
    with pytest.raises(FactWriteConflict):
        await store.repo.stale_due_fact(due, stale_fields(due, NOW))
    assert store.docs["f-x"]["state"] == "current"


async def test_staling_one_fact_leaves_the_others_alone(store):
    await seed(store, "x", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    await seed(store, "y", expires_at=PAST, expiry_kind=ExpiryKind.DATE)
    by_id = {d.fact_id: d for d in await store.repo.find_due_facts(NOW)}
    before = copy.deepcopy(store.docs["f-y"])
    await store.repo.stale_due_fact(by_id["f-x"], stale_fields(by_id["f-x"], NOW))
    assert store.docs["f-y"] == before


# ------------------------------------------------------------------------------------------------ stale_backlog

def stale(store, doc_id, *, account=ACC, since=None):
    over = {} if since is None else {"stale_since": since}
    put_raw(store, doc_id, doc_id, entity={"account_id": account, "state": FactState.STALE}, **over)


async def test_the_backlog_counts_stale_facts_per_account_with_the_oldest_stale_since(store):
    stale(store, "a1", since=NOW - timedelta(days=5))
    stale(store, "a2", since=NOW - timedelta(days=9))
    stale(store, "a3", since=NOW - timedelta(days=1))
    stale(store, "b1", account=OTHER, since=NOW)
    assert await store.repo.stale_backlog() == {ACC: (3, NOW - timedelta(days=9)), OTHER: (1, NOW)}


async def test_a_stale_fact_without_stale_since_counts_but_has_no_age(store):
    stale(store, "legacy")
    stale(store, "dated", since=NOW)
    stale(store, "other-legacy", account=OTHER)
    assert await store.repo.stale_backlog() == {ACC: (2, NOW), OTHER: (1, None)}


async def test_the_backlog_leaves_out_every_state_but_stale_and_an_empty_store_is_empty(store):
    assert await store.repo.stale_backlog() == {}
    await seed(store, "current")
    await seed(store, "archived", state=FactState.ARCHIVED)
    assert await store.repo.stale_backlog() == {}


async def test_a_legacy_stale_document_without_account_id_is_skipped_from_the_backlog(store):
    put_raw(store, "legacy", "legacy", state="stale", account_id=DROP, created_by_user_id=DROP, owner_id=ACC)
    stale(store, "fine", since=NOW)
    assert await store.repo.stale_backlog() == {ACC: (1, NOW)}
