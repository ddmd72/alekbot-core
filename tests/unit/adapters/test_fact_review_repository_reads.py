"""The review's reads and its one write in the fact repository (facts S4 · T3b): `get_stale_facts`, `get_recently_closed`,
the `state` of `get_paginated_facts`, and `archive_and_add`.

On the evaluating Firestore fake with the index refusal ON, so a query that needs a composite index the file does not
declare fails here. Assertions are on what a reader returns and on the raw stored documents.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import FactState
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_lifecycle import REVIEW_ACCEPTS, archive_fields
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, DROP, OTHER, T0, make_fact, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore, embed

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
EARLIER = {"at": T0, "from": None, "to": "current", "actor": "stage1", "reason": "created"}


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed(store, doc_id, *, state="current", account=ACC, **over):
    """A raw document of `doc_id` in `state`; a closed or stale state carries the flags a real one does."""
    flags = {"state": state, "is_current": True}
    if state in ("archived", "invalidated"):
        flags["valid_to"] = NOW
    return put_raw(store, doc_id, doc_id, entity={"account_id": account}, **{**flags, **over})


# ----------------------------------------------------------------------------------------------- get_stale_facts

async def test_the_stale_read_returns_the_accounts_stale_facts_only(store):
    seed(store, "s1", state="stale", stale_since=NOW)
    seed(store, "cur")
    seed(store, "arch", state="archived")
    seed(store, "theirs", state="stale", account=OTHER, stale_since=NOW)

    facts = await store.repo.get_stale_facts(ACC)

    assert [f.id for f in facts] == ["s1"]
    assert facts[0].state is FactState.STALE and facts[0].account_id == ACC


async def test_the_stale_read_has_a_list_vector_and_no_tags_or_metadata_vector(store):
    seed(store, "s1", state="stale", stale_since=NOW)

    (fact,) = await store.repo.get_stale_facts(ACC)

    assert type(fact.vector) is list and fact.vector == pytest.approx(embed("s1"))
    assert fact.tags_vector is None and fact.metadata_vector is None


async def test_the_stale_read_keeps_the_rest_of_the_document(store):
    seed(store, "s1", state="stale", stale_since=NOW, lifecycle_events=[EARLIER])

    (fact,) = await store.repo.get_stale_facts(ACC)

    assert fact.text == "s1" and fact.stale_since == NOW and fact.lifecycle_events == [EARLIER]
    assert fact.lineage_id == "lin-s1" and fact.domain is not None


async def test_the_stale_read_is_longest_stale_first_a_missing_date_first_then_the_id(store):
    seed(store, "b-new", state="stale", stale_since=NOW)
    seed(store, "c-old", state="stale", stale_since=NOW - timedelta(days=9))
    seed(store, "a-none", state="stale", stale_since=DROP)
    seed(store, "d-same", state="stale", stale_since=NOW)
    seed(store, "0-none", state="stale", stale_since=None)

    facts = await store.repo.get_stale_facts(ACC)

    assert [f.id for f in facts] == ["0-none", "a-none", "c-old", "b-new", "d-same"]


async def test_the_stale_read_of_an_account_without_stale_facts_is_empty(store):
    seed(store, "cur")

    assert await store.repo.get_stale_facts(ACC) == []


# ------------------------------------------------------------------------------------------ get_recently_closed

async def test_recently_closed_returns_archived_and_invalidated_but_not_other_states(store):
    seed(store, "arch", state="archived", valid_to=NOW - timedelta(days=1))
    seed(store, "inval", state="invalidated", valid_to=NOW - timedelta(days=2))
    seed(store, "sup", state="superseded", is_current=False, valid_to=NOW - timedelta(days=1))
    seed(store, "stale", state="stale", stale_since=NOW)
    seed(store, "cur")

    facts = await store.repo.get_recently_closed(ACC, NOW - timedelta(days=30), 10)

    assert [f.id for f in facts] == ["arch", "inval"]


async def test_recently_closed_keeps_the_window_inclusive_of_since(store):
    since = NOW - timedelta(days=5)
    seed(store, "on-edge", state="archived", valid_to=since)
    seed(store, "inside", state="archived", valid_to=since + timedelta(hours=1))
    seed(store, "before", state="invalidated", valid_to=since - timedelta(seconds=1))

    facts = await store.repo.get_recently_closed(ACC, since, 10)

    assert sorted(f.id for f in facts) == ["inside", "on-edge"]


async def test_recently_closed_is_newest_closure_first_and_capped_at_the_limit(store):
    for n in range(5):
        seed(store, f"c{n}", state="archived" if n % 2 else "invalidated", valid_to=NOW - timedelta(days=n))

    facts = await store.repo.get_recently_closed(ACC, NOW - timedelta(days=30), 3)

    assert [f.id for f in facts] == ["c0", "c1", "c2"]


async def test_recently_closed_never_returns_another_accounts_facts_or_any_vector(store):
    seed(store, "mine", state="archived")
    seed(store, "theirs", state="archived", account=OTHER)

    facts = await store.repo.get_recently_closed(ACC, NOW - timedelta(days=1), 10)

    assert [f.id for f in facts] == ["mine"]
    assert facts[0].vector is None and facts[0].tags_vector is None and facts[0].metadata_vector is None
    assert facts[0].valid_to == NOW and facts[0].state is FactState.ARCHIVED


# --------------------------------------------------------------------------------------- get_paginated_facts

async def test_paginated_facts_default_to_current(store):
    seed(store, "cur")
    seed(store, "stale", state="stale")

    facts, _ = await store.repo.get_paginated_facts(ACC)

    assert [f.id for f in facts] == ["cur"]


async def test_paginated_facts_read_the_stale_state_when_asked(store):
    seed(store, "cur")
    seed(store, "s1", state="stale", created_at=NOW)
    seed(store, "s2", state="stale", created_at=NOW - timedelta(days=1))
    seed(store, "theirs", state="stale", account=OTHER)

    first, cursor = await store.repo.get_paginated_facts(ACC, limit=1, state="stale")
    second, end = await store.repo.get_paginated_facts(ACC, limit=1, cursor_doc_id=cursor, state="stale")

    assert [f.id for f in first] == ["s1"] and cursor == "s1"
    assert [f.id for f in second] == ["s2"] and end is None


@pytest.mark.parametrize("state", ["archived", "invalidated", "superseded", "CURRENT", "", "any"])
async def test_paginated_facts_refuse_any_other_state(store, state):
    seed(store, "cur")

    with pytest.raises(ValueError):
        await store.repo.get_paginated_facts(ACC, state=state)


# ------------------------------------------------------------------------------------------- archive_and_add

async def stale_stored(store, doc_id="old", **over):
    seed(store, doc_id, state="stale", stale_since=NOW - timedelta(days=3), lifecycle_events=[EARLIER], **over)
    stored = await store.repo.resolve_current(doc_id, ACC, REVIEW_ACCEPTS)
    assert stored is not None
    return stored


def replacement(stored, **over):
    return make_fact("new", lineage_id="lin-new", lifecycle_events=[
        {"at": NOW, "from": None, "to": "current", "actor": "review", "reason": f"replaces {stored.fact.id}"}], **over)


async def test_archive_and_add_writes_both_documents(store):
    stored = await stale_stored(store)
    new = replacement(stored)

    await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "rewritten as f-new"), new)

    old = store.docs["old"]
    assert old["state"] == "archived" and old["is_current"] is True and old["valid_to"] == NOW
    assert old["last_updated"] == NOW and old["stale_since"] == NOW - timedelta(days=3)
    created = store.docs["f-new"]
    assert created["state"] == "current" and created["account_id"] == ACC and created["text"] == "new"
    assert created["context_priority_rank"] == 3


async def test_archive_and_add_stores_the_new_documents_vectors_as_vectors(store):
    stored = await stale_stored(store)

    await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    read = await store.repo.get_fact_by_id("f-new")
    assert read.vector == pytest.approx(embed("new"))
    assert read.tags_vector == pytest.approx(embed("t new")) and read.metadata_vector == pytest.approx(embed("m new"))


async def test_archive_and_add_writes_the_archived_trail_entry_and_keeps_the_new_ones_as_given(store):
    stored = await stale_stored(store)
    new = replacement(stored)

    await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "rewritten as f-new"), new)

    assert store.docs["old"]["lifecycle_events"] == [
        EARLIER, {"at": NOW, "from": "stale", "to": "archived", "actor": "review", "reason": "rewritten as f-new"}
    ]
    assert store.docs["f-new"]["lifecycle_events"] == new.lifecycle_events


async def test_archive_and_add_caps_the_archived_trail(store):
    events = [{**EARLIER, "reason": f"e{n}"} for n in range(20)]
    seed(store, "old", state="stale", lifecycle_events=events)
    stored = await store.repo.resolve_current("old", ACC, REVIEW_ACCEPTS)

    await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    trail = store.docs["old"]["lifecycle_events"]
    assert len(trail) == 20 and trail[0]["reason"] == "e1" and trail[-1]["to"] == "archived"


async def test_a_changed_token_gives_a_conflict_and_nothing_is_written(store):
    stored = await stale_stored(store)
    store.interfere("old", {"tags": ["moved"]})   # another writer commits when the transaction reads the document
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactWriteConflict):
        await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    assert store.docs["old"] == {**before["old"], "tags": ["moved"]} and "f-new" not in store.docs


async def test_a_token_that_was_stale_before_the_call_gives_a_conflict_and_nothing_is_written(store):
    stored = await stale_stored(store)
    await store.repo.update_fact_fields(stored, {"last_updated": NOW})
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactWriteConflict):
        await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    assert store.docs == before


async def test_a_vanished_archived_document_gives_a_conflict(store):
    stored = await stale_stored(store)
    del store.docs["old"]

    with pytest.raises(FactWriteConflict):
        await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    assert "f-new" not in store.docs


async def test_a_new_fact_of_another_account_is_refused_before_any_write(store):
    stored = await stale_stored(store)
    before = copy.deepcopy(store.docs)

    with pytest.raises(ValueError):
        await store.repo.archive_and_add(
            stored, archive_fields(stored.fact, NOW, "review", "r"),
            replacement(stored, account=OTHER),
        )

    assert store.docs == before


@pytest.mark.parametrize("missing", ["vector", "tags_vector", "metadata_vector"])
async def test_a_new_fact_without_its_three_vectors_is_refused(store, missing):
    stored = await stale_stored(store)
    before = copy.deepcopy(store.docs)

    with pytest.raises(ValueError, match=missing):
        await store.repo.archive_and_add(
            stored, archive_fields(stored.fact, NOW, "review", "r"),
            replacement(stored, **{missing: None}),
        )

    assert store.docs == before


async def test_a_hybrid_stored_document_is_refused_with_permission_error_and_nothing_is_written(store):
    put_raw(store, "hybrid", "hybrid", account_id=OTHER, owner_id=ACC, created_by_user_id=DROP, state="stale")
    stored = await store.repo.resolve_current("hybrid", ACC, REVIEW_ACCEPTS)
    assert stored is not None and stored.fact.account_id == ACC     # the read's view: the owner_id compatibility mapping
    before = copy.deepcopy(store.docs)

    with pytest.raises(PermissionError, match="Not authorized to modify this fact"):
        await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    assert store.docs == before


async def test_a_stored_fact_read_as_another_account_is_refused(store):
    stored = await stale_stored(store)
    as_other = StoredFact(stored.fact.model_copy(update={"account_id": OTHER}), stored.update_time)
    before = copy.deepcopy(store.docs)

    with pytest.raises(PermissionError):
        await store.repo.archive_and_add(
            as_other, archive_fields(as_other.fact, NOW, "review", "r"),
            replacement(as_other, account=OTHER),
        )

    assert store.docs == before


async def test_a_new_id_that_already_exists_fails_the_commit_and_archives_nothing(store):
    stored = await stale_stored(store)
    seed(store, "f-new")
    before = copy.deepcopy(store.docs)

    with pytest.raises(Exception):
        await store.repo.archive_and_add(stored, archive_fields(stored.fact, NOW, "review", "r"), replacement(stored))

    assert store.docs == before
