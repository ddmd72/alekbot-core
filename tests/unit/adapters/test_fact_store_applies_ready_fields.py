"""The fact stores apply the fields the caller built (facts S4 · T3c; S4 inward review L2, S1–S3 inward review L3):
`archive_and_add(archived, fields, new_fact)`, `write_superseding(new_fact, [(stored, fields), ...])` and
`stale_due_fact(due, fields)` write the given fields as they are, under their preconditions (IN_PLACE_FIELDS, the read
token, tenancy for the archive), and decide nothing.

Both stores: the Firestore repository on the evaluating fake, and the dry-run kit's overlay over it.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactState
from src.domain.exceptions import FactWriteConflict, InPlaceFieldRefused
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS, REVIEW_ACCEPTS, archive_fields, superseded_closure
from src.domain.fact_lifecycle_sweep import stale_fields
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, T0, make_fact, put_raw
from tests.support.fact_store import FactStore

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
OTHER = "acc-other"
# A trail entry no domain builder would write for these arguments: the store must store it, not rebuild its own.
GIVEN_ENTRY = {"at": NOW, "from": "stale", "to": "archived", "actor": "owner", "reason": "as the caller built it"}


@pytest.fixture
def store():
    return FactStore()


@pytest.fixture(params=["firestore", "kit"])
def repo(request, store):
    return store.repo if request.param == "firestore" else NoWriteFactRepository(store.repo)


async def stale(store, repo, doc_id="old"):
    put_raw(store, doc_id, doc_id, state="stale", stale_since=T0, lifecycle_events=[])
    stored = await repo.resolve_current(doc_id, ACC, REVIEW_ACCEPTS)
    assert stored is not None
    return stored


async def current(store, repo, doc_id):
    put_raw(store, doc_id, doc_id, state="current", lifecycle_events=[])
    stored = await repo.resolve_current(doc_id, ACC, CONSOLIDATION_ACCEPTS)
    assert stored is not None
    return stored


async def read(repo, doc_id):
    return await repo.get_fact_by_id(doc_id)


def given_archive():
    return {"state": FactState.ARCHIVED, "is_current": True, "valid_to": NOW, "last_updated": NOW,
            "lifecycle_events": [GIVEN_ENTRY]}


# ------------------------------------------------------------------------------------------------ archive_and_add

async def test_archive_and_add_writes_the_given_fields_as_they_are(store, repo):
    stored = await stale(store, repo)

    await repo.archive_and_add(stored, given_archive(), make_fact("new", lineage_id="lin-new"))

    old = await read(repo, "old")
    assert old.state is FactState.ARCHIVED and old.valid_to == NOW and old.lifecycle_events == [GIVEN_ENTRY]
    assert old.stale_since == T0     # not named by the fields, so not touched
    assert (await read(repo, "f-new")).state is FactState.CURRENT


async def test_archive_and_add_with_the_domain_archive_writes_exactly_it(store, repo):
    stored = await stale(store, repo)
    fields = archive_fields(stored.fact, NOW, "owner", "ended q=q7 (digit)")

    await repo.archive_and_add(stored, fields, make_fact("new", lineage_id="lin-new"))

    old = await read(repo, "old")
    assert {name: getattr(old, name) for name in fields} == fields


async def test_archive_and_add_refuses_a_field_outside_in_place_fields_before_any_write(store, repo):
    stored = await stale(store, repo)
    before = copy.deepcopy(store.docs)

    with pytest.raises(InPlaceFieldRefused):
        await repo.archive_and_add(stored, {**given_archive(), "text": "rewritten"}, make_fact("new"))

    assert store.docs == before and await read(repo, "f-new") is None


async def test_archive_and_add_refuses_a_changed_token(store, repo):
    stored = await stale(store, repo)
    await repo.update_fact_fields(stored, {"last_updated": NOW})     # the token `stored` was read with is gone

    with pytest.raises(FactWriteConflict):
        await repo.archive_and_add(stored, given_archive(), make_fact("new"))

    assert (await read(repo, "old")).state is FactState.STALE and await read(repo, "f-new") is None


async def test_archive_and_add_refuses_a_new_fact_of_another_account(store, repo):
    stored = await stale(store, repo)

    with pytest.raises(ValueError):
        await repo.archive_and_add(stored, given_archive(), make_fact("new", account=OTHER))

    assert (await read(repo, "old")).state is FactState.STALE and await read(repo, "f-new") is None


async def test_archive_and_add_refuses_a_document_stored_for_another_account(store, repo):
    stored = await stale(store, repo)
    if isinstance(repo, NoWriteFactRepository):
        # The kit sees another account's document only in its overlay: put it there, then read it back.
        await repo.update_fact_fields(stored, {"last_updated": NOW})
        stored = await repo.resolve_current("old", ACC, REVIEW_ACCEPTS)
    as_other = StoredFact(stored.fact.model_copy(update={"account_id": OTHER}), stored.update_time)

    with pytest.raises(PermissionError):
        await repo.archive_and_add(as_other, given_archive(), make_fact("new", account=OTHER))

    assert (await read(repo, "old")).state is FactState.STALE and await read(repo, "f-new") is None


# ------------------------------------------------------------------------------------------------ write_superseding

async def test_write_superseding_writes_each_given_closure(store, repo):
    a, b = await current(store, repo, "a"), await current(store, repo, "b")
    merged = make_fact("merged", lineage_id="lin-merged")
    closures = [
        (a, superseded_closure(a.fact, merged, NOW, "stage2a")),
        (b, {**superseded_closure(b.fact, merged, NOW, "stage2a"), "lifecycle_events": [GIVEN_ENTRY]}),
    ]

    await repo.write_superseding(merged, closures)

    stored_a, stored_b = await read(repo, "a"), await read(repo, "b")
    assert stored_a.state is FactState.SUPERSEDED and stored_a.superseded_by == "f-merged"
    assert stored_a.lifecycle_events == closures[0][1]["lifecycle_events"]
    assert stored_b.lifecycle_events == [GIVEN_ENTRY]     # as given, not rebuilt from an actor
    assert (await read(repo, "f-merged")).state is FactState.CURRENT


async def test_write_superseding_refuses_a_field_outside_in_place_fields_before_any_write(store, repo):
    a = await current(store, repo, "a")
    merged = make_fact("merged", lineage_id="lin-merged")
    before = copy.deepcopy(store.docs)

    with pytest.raises(InPlaceFieldRefused):
        await repo.write_superseding(merged, [(a, {**superseded_closure(a.fact, merged, NOW, "stage1"), "tags": []})])

    assert store.docs == before and await read(repo, "f-merged") is None


async def test_write_superseding_refuses_a_changed_token(store, repo):
    a = await current(store, repo, "a")
    await repo.update_fact_fields(a, {"last_updated": NOW + timedelta(seconds=1)})
    merged = make_fact("merged", lineage_id="lin-merged")

    with pytest.raises(FactWriteConflict):
        await repo.write_superseding(merged, [(a, superseded_closure(a.fact, merged, NOW, "stage1"))])

    assert (await read(repo, "a")).state is FactState.CURRENT and await read(repo, "f-merged") is None


async def test_write_superseding_refuses_no_closures(store, repo):
    with pytest.raises(ValueError):
        await repo.write_superseding(make_fact("merged"), [])


# ------------------------------------------------------------------------------------------------ stale_due_fact (S1–S3 L3)

async def due_fact(store, repo, doc_id="due"):
    put_raw(store, doc_id, doc_id, state="current", expiry_kind="horizon", ttl_days=30, expires_at=T0 + timedelta(days=30),
            lifecycle_events=[{"at": T0, "from": None, "to": "current", "actor": "stage1", "reason": "born"}])
    (due,) = [d for d in await repo.find_due_facts(NOW) if d.fact_id == doc_id]
    return due


async def test_stale_due_fact_writes_the_stale_fields_of_the_projected_trail(store, repo):
    due = await due_fact(store, repo)
    fields = stale_fields(due, NOW)

    await repo.stale_due_fact(due, fields)

    stored = await read(repo, "due")
    assert stored.state is FactState.STALE and stored.stale_since == NOW
    assert stored.lifecycle_events == fields["lifecycle_events"]
    assert [e["reason"] for e in stored.lifecycle_events] == ["born", f"expired {due.expires_at.isoformat()}"]


async def test_stale_due_fact_writes_the_given_fields_as_they_are(store, repo):
    due = await due_fact(store, repo)
    given = {**stale_fields(due, NOW), "lifecycle_events": [GIVEN_ENTRY]}

    await repo.stale_due_fact(due, given)

    assert (await read(repo, "due")).lifecycle_events == [GIVEN_ENTRY]     # applied, not rebuilt from a re-read trail


async def test_stale_due_fact_refuses_a_field_outside_in_place_fields_before_any_write(store, repo):
    due = await due_fact(store, repo)
    before = copy.deepcopy(store.docs)

    with pytest.raises(InPlaceFieldRefused):
        await repo.stale_due_fact(due, {**stale_fields(due, NOW), "text": "x"})

    assert store.docs == before and (await read(repo, "due")).state is FactState.CURRENT


async def test_stale_due_fact_refuses_a_changed_token(store, repo):
    due = await due_fact(store, repo)
    stored = await repo.resolve_current("due", ACC, CONSOLIDATION_ACCEPTS)
    await repo.update_fact_fields(stored, {"last_updated": NOW})

    with pytest.raises(FactWriteConflict):
        await repo.stale_due_fact(due, stale_fields(due, NOW))

    assert (await read(repo, "due")).state is FactState.CURRENT
