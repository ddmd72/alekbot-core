"""Facts S4 · T3b: the dry-run kit's overlay store answers the review's reads and its archive-and-add write.

The live store is the evaluating Firestore fake behind the real repository, with a guard that refuses every live write;
the kit's `NoWriteFactRepository` runs on top. Assertions are on what the kit's reads return, on the overlay and on the
live documents staying byte-identical.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactState
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_lifecycle import REVIEW_ACCEPTS, lifecycle_fields
from src.domain.stored_fact import StoredFact
from src.ports.repository import FactRepository
from tests.support.fact_kit import ACC, OTHER, USER, make_fact, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore, embed

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=30)
LIVE_WRITES = frozenset({
    "add_fact", "add_fact_if_unique", "write_superseding", "update_fact_fields", "stale_due_fact",
    "archive_and_add", "refresh_biographical_context_cache",
})


def _forwarded(name):
    async def read(self, *args, **kwargs):
        await asyncio.sleep(0)
        return await getattr(self._real, name)(*args, **kwargs)
    return read


def _refused(name):
    async def write(self, *args, **kwargs):
        raise AssertionError(f"a live write reached the real repository: {name}")
    return write


GuardedRepo = type("GuardedRepo", (FactRepository,), {
    "__init__": lambda self, real: setattr(self, "_real", real),
    **{n: (_refused(n) if n in LIVE_WRITES else _forwarded(n)) for n in FactRepository.__abstractmethods__},
})


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture
def kit(store):
    return NoWriteFactRepository(GuardedRepo(store.repo))


def seed(store, doc_id, *, state="current", account=ACC, **over):
    flags = {"state": state, "is_current": True}
    if state in ("archived", "invalidated"):
        flags["valid_to"] = NOW - timedelta(days=1)
    return put_raw(store, doc_id, doc_id, entity={"account_id": account}, **{**flags, **over})


def live_state(store):
    return {name: (copy.deepcopy(col.docs), dict(col.versions)) for name, col in store.db.cols.items() if col.docs}


async def read(kit, doc_id):
    stored = await kit.resolve_current(doc_id, ACC, frozenset(FactState))
    assert stored is not None
    return stored


# ---------------------------------------------------------------------------------------------- get_stale_facts

async def test_a_fact_the_run_staled_is_in_the_stale_list_in_order(store, kit):
    seed(store, "live-old", state="stale", stale_since=NOW - timedelta(days=9))
    seed(store, "cur")
    stored = await read(kit, "cur")

    await kit.update_fact_fields(stored, {**lifecycle_fields(FactState.STALE, NOW), "stale_since": NOW - timedelta(days=2)})

    assert [f.id for f in await kit.get_stale_facts(ACC)] == ["live-old", "cur"]


async def test_a_fact_staged_back_to_current_leaves_the_stale_list(store, kit):
    seed(store, "s1", state="stale", stale_since=NOW)
    seed(store, "s2", state="stale", stale_since=NOW)
    stored = await read(kit, "s1")

    await kit.update_fact_fields(stored, {**lifecycle_fields(FactState.CURRENT, NOW), "stale_since": None})

    assert [f.id for f in await kit.get_stale_facts(ACC)] == ["s2"]


async def test_a_stale_fact_the_run_changed_shows_the_overlay_document(store, kit):
    seed(store, "s1", state="stale", stale_since=NOW)
    stored = await read(kit, "s1")

    await kit.update_fact_fields(stored, {"last_updated": NOW + timedelta(hours=1)})

    (fact,) = await kit.get_stale_facts(ACC)
    assert fact.last_updated == NOW + timedelta(hours=1)


async def test_the_stale_list_of_the_kit_has_the_stores_projection_and_never_another_accounts_facts(store, kit):
    seed(store, "mine", state="stale", stale_since=NOW)
    await kit.add_fact(make_fact("mine-staged", state=FactState.STALE, stale_since=NOW))
    await kit.add_fact(make_fact("theirs", account=OTHER, state=FactState.STALE, stale_since=NOW))

    facts = await kit.get_stale_facts(ACC)

    assert [f.id for f in facts] == ["f-mine-staged", "mine"]
    assert all(f.tags_vector is None and f.metadata_vector is None for f in facts)
    assert all(f.vector is not None for f in facts)


async def test_the_stale_list_with_no_overlay_is_the_live_one(store, kit):
    seed(store, "s1", state="stale", stale_since=NOW)
    seed(store, "s0", state="stale", stale_since=None)

    assert [f.id for f in await kit.get_stale_facts(ACC)] == [f.id for f in await store.repo.get_stale_facts(ACC)] == ["s0", "s1"]


# ------------------------------------------------------------------------------------------ get_recently_closed

async def test_a_fact_the_run_archived_is_recently_closed_and_a_live_closed_one_stays(store, kit):
    seed(store, "live-arch", state="archived", valid_to=NOW - timedelta(days=3))
    seed(store, "s1", state="stale", stale_since=NOW)
    stored = await read(kit, "s1")
    await kit.update_fact_fields(stored, {**lifecycle_fields(FactState.ARCHIVED, NOW), "last_updated": NOW})

    facts = await kit.get_recently_closed(ACC, SINCE, 10)

    assert [f.id for f in facts] == ["s1", "live-arch"]
    assert all(f.vector is None for f in facts)


async def test_a_closed_fact_the_run_revived_leaves_recently_closed(store, kit):
    seed(store, "arch", state="archived")
    stored = await read(kit, "arch")

    await kit.update_fact_fields(stored, lifecycle_fields(FactState.CURRENT, NOW))

    assert await kit.get_recently_closed(ACC, SINCE, 10) == []


async def test_recently_closed_keeps_the_window_the_limit_and_the_order_over_live_and_overlay(store, kit):
    for n in range(3):
        seed(store, f"live{n}", state="archived", valid_to=NOW - timedelta(days=n + 2))
    seed(store, "old", state="invalidated", valid_to=SINCE - timedelta(days=1))
    seed(store, "s1", state="stale", stale_since=NOW)
    await kit.update_fact_fields(await read(kit, "s1"), lifecycle_fields(FactState.INVALIDATED, NOW))

    facts = await kit.get_recently_closed(ACC, SINCE, 2)

    assert [f.id for f in facts] == ["s1", "live0"]


async def test_the_overlay_does_not_crowd_a_live_row_out_of_the_limit(store, kit):
    seed(store, "live0", state="archived", valid_to=NOW - timedelta(days=5))
    seed(store, "live1", state="archived", valid_to=NOW - timedelta(days=6))
    seed(store, "s1", state="stale", stale_since=NOW)
    await kit.update_fact_fields(await read(kit, "s1"), lifecycle_fields(FactState.ARCHIVED, NOW))
    await kit.update_fact_fields(await read(kit, "live0"), {"last_updated": NOW})      # live0 is in the overlay now

    facts = await kit.get_recently_closed(ACC, SINCE, 3)

    assert [f.id for f in facts] == ["s1", "live0", "live1"]


async def test_recently_closed_of_the_kit_never_shows_another_account(store, kit):
    seed(store, "theirs", state="archived", account=OTHER)
    await kit.add_fact(make_fact("t", account=OTHER, state=FactState.ARCHIVED, valid_to=NOW))

    assert await kit.get_recently_closed(ACC, SINCE, 10) == []


# ---------------------------------------------------------------------------------------- get_paginated_facts

async def test_the_paginated_state_is_forwarded_to_the_live_store(store, kit):
    seed(store, "cur")
    seed(store, "s1", state="stale")

    stale, _ = await kit.get_paginated_facts(ACC, state="stale")
    current, _ = await kit.get_paginated_facts(ACC)

    assert [f.id for f in stale] == ["s1"] and [f.id for f in current] == ["cur"]
    with pytest.raises(ValueError):
        await kit.get_paginated_facts(ACC, state="archived")


# --------------------------------------------------------------------------------------------- search_facts

async def test_a_search_hit_the_run_closed_is_dropped_and_the_next_hit_takes_its_place(store, kit):
    for text in ("alpha", "beta", "gamma"):
        await store.repo.add_fact(make_fact(text))
    query = embed("alpha")
    assert [f.id for f in await kit.search_facts(query, limit=2, user_id=USER, account_id=ACC)][0] == "f-alpha"

    await kit.update_fact_fields(await read(kit, "f-alpha"), lifecycle_fields(FactState.ARCHIVED, NOW))
    hits = await kit.search_facts(query, limit=2, user_id=USER, account_id=ACC)

    assert "f-alpha" not in [f.id for f in hits] and len(hits) == 2


async def test_a_search_hit_the_run_only_changed_stays(store, kit):
    await store.repo.add_fact(make_fact("alpha"))
    await kit.update_fact_fields(await read(kit, "f-alpha"), {"last_updated": NOW})

    hits = await kit.search_facts(embed("alpha"), limit=3, user_id=USER, account_id=ACC)

    assert [f.id for f in hits] == ["f-alpha"]


# --------------------------------------------------------------------------------------------- archive_and_add

async def staged(store, kit):
    seed(store, "old", state="stale", stale_since=NOW - timedelta(days=3))
    stored = await kit.resolve_current("old", ACC, REVIEW_ACCEPTS)
    new = make_fact("new", lineage_id="lin-new")
    return stored, new


async def test_archive_and_add_is_recorded_and_leaves_the_live_store_byte_identical(store, kit):
    stored, new = await staged(store, kit)
    before = live_state(store)

    await kit.archive_and_add(stored, new, NOW, actor="review", reason="rewritten as f-new")

    assert live_state(store) == before
    assert kit.writes == [{"kind": "archive_and_add", "archived_id": "old", "new_id": "f-new", "actor": "review"}]


async def test_the_overlay_holds_the_archived_fact_and_the_new_one(store, kit):
    stored, new = await staged(store, kit)

    await kit.archive_and_add(stored, new, NOW, actor="review", reason="rewritten as f-new")

    old = kit.overlay_document("old")
    assert old.state is FactState.ARCHIVED and old.valid_to == NOW and old.stale_since == NOW - timedelta(days=3)
    assert old.lifecycle_events[-1] == {"at": NOW, "from": "stale", "to": "archived", "actor": "review", "reason": "rewritten as f-new"}
    assert kit.overlay_document("f-new").state is FactState.CURRENT
    assert [f.id for f in await kit.get_stale_facts(ACC)] == []
    assert [f.id for f in await kit.get_recently_closed(ACC, SINCE, 5)] == ["old"]


async def test_a_second_archive_of_the_same_token_is_a_conflict(store, kit):
    stored, new = await staged(store, kit)
    await kit.archive_and_add(stored, new, NOW, actor="review", reason="r")

    with pytest.raises(FactWriteConflict):
        await kit.archive_and_add(stored, make_fact("other", lineage_id="lin-other"), NOW, actor="review", reason="r")

    assert [w["kind"] for w in kit.writes] == ["archive_and_add"] and kit.overlay_document("f-other") is None


async def test_archive_and_add_refuses_another_account_missing_vectors_and_a_taken_id_and_writes_nothing(store, kit):
    stored, new = await staged(store, kit)

    with pytest.raises(ValueError):
        await kit.archive_and_add(stored, make_fact("x", account=OTHER), NOW, actor="review", reason="r")
    with pytest.raises(ValueError, match="tags_vector"):
        await kit.archive_and_add(stored, make_fact("y", tags_vector=None), NOW, actor="review", reason="r")
    with pytest.raises(ValueError, match="already exists"):
        await kit.archive_and_add(stored, make_fact("n", id="old"), NOW, actor="review", reason="r")

    assert kit.writes == [] and kit.overlay_document("old") is None


async def test_archive_and_add_refuses_a_stored_fact_read_as_another_account(store, kit):
    await kit.add_fact(make_fact("k", state=FactState.STALE, stale_since=NOW))     # in the overlay: the kit reads its tenancy there
    stored = await read(kit, "f-k")
    as_other = StoredFact(stored.fact.model_copy(update={"account_id": OTHER}), stored.update_time)

    with pytest.raises(PermissionError, match="Not authorized to modify this fact"):
        await kit.archive_and_add(as_other, make_fact("x", account=OTHER), NOW, actor="review", reason="r")

    assert [w["kind"] for w in kit.writes] == ["add_fact"] and kit.overlay_document("f-x") is None
