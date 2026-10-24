"""The trail and the expiry projection in the fact repository (Facts S3 · T3): the closure entry `write_superseding` writes
on every replaced document, the entry of the Cabinet's delete (`CabinetFactService.invalidate`, facts S4 T2c), and the expiry decision the search reads return.

On the evaluating Firestore fake with the index refusal ON; assertions are on the raw stored documents.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS, superseded_closure
from src.services.cabinet_fact_service import CabinetFactService
from tests.support.fact_kit import ACC, T0, USER, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
EARLIER = {"at": T0, "from": None, "to": "current", "actor": "stage1", "reason": "version 2 of old"}


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def seed(store, text, **over):
    fact = make_fact(text, **over)
    await store.repo.add_fact(fact)
    return fact


async def read(store, fact_id, accepted=CONSOLIDATION_ACCEPTS):
    return await store.repo.resolve_current(fact_id, ACC, accepted)


def version_of(stored, new_id):
    f = stored.fact
    return f.model_copy(update=dict(id=new_id, version=f.version + 1, replaces_fact_id=f.id, valid_from=NOW,
                                    last_updated=NOW, superseded_by=None))


def closed_by(new, replaced, actor):
    """The closures a caller builds (`superseded_closure`), which the repository applies."""
    return [(stored, superseded_closure(stored.fact, new, NOW, actor)) for stored in replaced]


def closing_event(doc):
    return doc["lifecycle_events"][-1]


# ------------------------------------------------------------------------------------------ write_superseding

async def test_a_version_closes_the_old_document_with_a_replaced_by_entry(store):
    await seed(store, "old")
    stored = await read(store, "f-old")

    new = version_of(stored, "f-new")
    await store.repo.write_superseding(new, closed_by(new, [stored], "stage1"))

    assert store.docs["f-old"]["lifecycle_events"] == [
        {"at": NOW, "from": "current", "to": "superseded", "actor": "stage1", "reason": "replaced by f-new"}
    ]


async def test_the_closure_entry_extends_the_trail_the_document_already_had(store):
    await seed(store, "old", lifecycle_events=[EARLIER])
    stored = await read(store, "f-old")

    new = version_of(stored, "f-new")
    await store.repo.write_superseding(new, closed_by(new, [stored], "stage2a"))

    trail = store.docs["f-old"]["lifecycle_events"]
    assert trail[0] == EARLIER and len(trail) == 2
    assert closing_event(store.docs["f-old"])["actor"] == "stage2a"


async def test_the_new_version_is_stored_as_given_its_trail_is_the_callers(store):
    await seed(store, "old", lifecycle_events=[EARLIER])
    stored = await read(store, "f-old")
    own = {"at": NOW, "from": None, "to": "current", "actor": "stage1", "reason": "version 2 of f-old"}
    new = version_of(stored, "f-new").model_copy(update={"lifecycle_events": [EARLIER, own]})

    await store.repo.write_superseding(new, closed_by(new, [stored], "stage1"))

    assert store.docs["f-new"]["lifecycle_events"] == [EARLIER, own]


async def test_a_merge_closes_each_source_with_a_merged_into_entry_on_its_own_trail(store):
    await seed(store, "a", lifecycle_events=[EARLIER])
    await seed(store, "b", state=FactState.STALE)
    sources = [await read(store, "f-a"), await read(store, "f-b", accepted=frozenset(FactState))]
    merged = make_fact("merged")

    await store.repo.write_superseding(merged, closed_by(merged, sources, "stage2a"))

    a, b = store.docs["f-a"]["lifecycle_events"], store.docs["f-b"]["lifecycle_events"]
    assert a[0] == EARLIER and len(a) == 2
    assert a[-1] == {"at": NOW, "from": "current", "to": "superseded", "actor": "stage2a", "reason": "merged into f-merged"}
    assert b == [{"at": NOW, "from": "stale", "to": "superseded", "actor": "stage2a", "reason": "merged into f-merged"}]
    assert store.docs["f-merged"]["lifecycle_events"] == []


async def test_the_closure_changes_the_old_document_in_its_lifecycle_fields_and_the_trail_only(store):
    await seed(store, "old")
    stored = await read(store, "f-old")
    before = copy.deepcopy(store.docs["f-old"])

    new = version_of(stored, "f-new")
    await store.repo.write_superseding(new, closed_by(new, [stored], "stage1"))

    after = store.docs["f-old"]
    assert {k for k in after if after[k] != before[k]} == {
        "state", "is_current", "valid_to", "last_updated", "superseded_by", "lifecycle_events"}


async def test_the_actor_is_required(store):
    # The closure is the caller's now (superseded_closure); the rule that it names its actor moved there with it.
    await seed(store, "old")
    stored = await read(store, "f-old")
    with pytest.raises(TypeError):
        superseded_closure(stored.fact, version_of(stored, "f-new"), NOW)


# ------------------------------------------------------------------------------------------- the Cabinet delete (CabinetFactService over this repository; facts S4 T2c, reviewer edit)

async def test_the_cabinet_invalidate_writes_its_entry(store):
    await seed(store, "x", lifecycle_events=[EARLIER])
    started = datetime.now(timezone.utc)

    await CabinetFactService(store.repo).invalidate("f-x", ACC)

    trail = store.docs["f-x"]["lifecycle_events"]
    assert trail[0] == EARLIER and len(trail) == 2
    entry = trail[1]
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == (
        "current", "invalidated", "cabinet", "deleted in the Cabinet")
    assert started <= entry["at"] <= datetime.now(timezone.utc) and entry["at"] == store.docs["f-x"]["valid_to"]


async def test_invalidating_a_stale_fact_records_stale_as_the_from_state(store):
    await seed(store, "x", state=FactState.STALE)
    await CabinetFactService(store.repo).invalidate("f-x", ACC)
    assert closing_event(store.docs["f-x"])["from"] == "stale"


async def test_invalidating_through_a_superseded_id_writes_the_entry_on_the_current_version_only(store):
    await seed(store, "old")
    stored = await read(store, "f-old")
    new = version_of(stored, "f-new")
    await store.repo.write_superseding(new, closed_by(new, [stored], "stage1"))
    old_trail = copy.deepcopy(store.docs["f-old"]["lifecycle_events"])

    await CabinetFactService(store.repo).invalidate("f-old", ACC)

    assert store.docs["f-old"]["lifecycle_events"] == old_trail
    assert closing_event(store.docs["f-new"])["actor"] == "cabinet"
    assert store.docs["f-new"]["state"] == "invalidated"


# --------------------------------------------------------------------------------- search returns the decision

async def test_search_returns_the_stored_expiry_decision(store):
    due = T0 + timedelta(days=30)
    fact = await seed(store, "x", expiry_kind=ExpiryKind.HORIZON, ttl_days=30, expires_at=due)
    (got,) = await store.repo.search_facts(fact.vector, "vector", user_id=USER, account_id=ACC)
    assert (got.expiry_kind, got.ttl_days, got.expires_at) == (ExpiryKind.HORIZON, 30, due)


async def test_search_returns_a_fact_written_before_the_expiry_model_with_no_decision(store):
    fact = await seed(store, "x")
    (got,) = await store.repo.search_facts(fact.vector, "vector", user_id=USER, account_id=ACC)
    assert (got.expiry_kind, got.ttl_days, got.expires_at) == (None, None, None)


async def test_search_leaves_the_trail_and_stale_since_at_their_defaults(store):
    fact = await seed(store, "x", expiry_kind=ExpiryKind.DATE, expires_at=NOW, lifecycle_events=[EARLIER])
    (got,) = await store.repo.search_facts(fact.vector, "vector", user_id=USER, account_id=ACC)
    assert (got.stale_since, got.lifecycle_events) == (None, [])


async def test_the_domain_search_returns_the_decision_too(store):
    due = T0 + timedelta(days=3)
    await seed(store, "x", expiry_kind=ExpiryKind.DATE, expires_at=due)
    (got,) = await store.repo.search_facts_by_domain(["health"], account_id=ACC)
    assert (got.expiry_kind, got.expires_at) == (ExpiryKind.DATE, due)
