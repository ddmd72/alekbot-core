"""FactRepository version kit (Facts S2 · T2): `resolve_current`, `write_superseding`, `update_fact_fields`, Cabinet
`invalidate_fact` via resolution, `get_lineage` and `get_latest_fact_by_lineage`, on the evaluating Firestore fake with the
index refusal ON (`config/firestore.indexes.json`).

Every document is seeded through a real write (`add_fact` or a field `update`), never by assigning into `col.docs`:
documents placed straight into the fake all carry the same `update_time` token, which hides a conflict.
"""
import copy
from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import Aborted, AlreadyExists
from google.cloud.firestore_v1.vector import Vector

from scripts.data_audit.facts import check_fact, check_lineages
from src.domain.entities import FactEntity, FactState
from src.domain.exceptions import FactResolutionError, FactWriteConflict, InPlaceFieldRefused
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS
from tests.support.fact_kit import ACC, OTHER, T0, USER, make_fact
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def seed(store, text, **over):
    fact = make_fact(text, **over)
    await store.repo.add_fact(fact)
    return fact


async def seed_closed(store, text, successor, **over):
    return await seed(store, text, state=FactState.SUPERSEDED, is_current=False, valid_to=T0,
                      superseded_by=successor, **over)


async def resolve(store, fact_id, accepted=CONSOLIDATION_ACCEPTS, account=ACC):
    return await store.repo.resolve_current(fact_id, account, accepted)


def version_of(stored, new_id, text):
    f = stored.fact
    return f.model_copy(update=dict(
        id=new_id, text=text, version=f.version + 1, replaces_fact_id=f.id, valid_from=NOW, last_updated=NOW,
        vector=embed(text), tags_vector=embed("t " + text), metadata_vector=embed("m " + text),
    ))


def stamp(store, doc_id):
    return store.db.collection(FACTS).versions[doc_id]


# ------------------------------------------------------------------------------------------ write_superseding

async def test_write_superseding_creates_the_new_doc_and_closes_every_replaced_doc_in_one_commit(store):
    await seed(store, "a")
    await seed(store, "b")
    sources = [await resolve(store, "f-a"), await resolve(store, "f-b")]
    merged = make_fact("merged")

    await store.repo.write_superseding(merged, sources, NOW, actor="stage2a")

    assert store.docs["f-merged"]["text"] == "merged"
    for source_id in ("f-a", "f-b"):
        doc = store.docs[source_id]
        assert doc["state"] == "superseded" and type(doc["state"]) is str
        assert doc["is_current"] is False and doc["valid_to"] == NOW and doc["last_updated"] == NOW
        assert doc["superseded_by"] == "f-merged"
        # Reviewer edit (facts S3, T3; plan §3.8): `actor` is now a required keyword, and each closed document gets
        # its own trail entry in the same commit.
        assert doc["lifecycle_events"] == [{"at": NOW, "from": "current", "to": "superseded", "actor": "stage2a",
                                            "reason": "merged into f-merged"}]
    assert stamp(store, "f-merged") == stamp(store, "f-a") == stamp(store, "f-b")


async def test_write_superseding_checks_each_source_token_against_its_own_snapshot(store):
    for name in "abc":
        await seed(store, name)
    sources = [await resolve(store, f"f-{n}") for n in "abc"]
    before = {k: copy.deepcopy(v) for k, v in store.docs.items()}
    store.interfere("f-b", {"text": "changed elsewhere"})   # interleaving source: a commit lands on b at its transactional read

    with pytest.raises(FactWriteConflict) as caught:
        await store.repo.write_superseding(make_fact("merged"), sources, NOW, actor="stage2a")

    assert caught.value.fact_id == "f-b"
    assert store.docs["f-b"]["text"] == "changed elsewhere"     # the interference happened
    assert store.docs["f-b"]["state"] == "current"
    assert "f-merged" not in store.docs
    assert store.docs["f-a"] == before["f-a"] and store.docs["f-c"] == before["f-c"]


async def test_write_superseding_on_a_changed_doc_raises_conflict_and_writes_nothing(store):
    await seed(store, "old")
    stale = await resolve(store, "f-old")
    await store.repo.facts_col.document("f-old").update({"text": "edited since the read"})
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactWriteConflict) as caught:
        await store.repo.write_superseding(version_of(stale, "f-new", "new"), [stale], NOW, actor="stage1")

    assert caught.value.fact_id == "f-old"
    assert store.docs == before


async def test_write_superseding_on_a_deleted_doc_raises_conflict(store):
    await seed(store, "old")
    stale = await resolve(store, "f-old")
    await store.repo.facts_col.document("f-old").delete()

    with pytest.raises(FactWriteConflict):
        await store.repo.write_superseding(version_of(stale, "f-new", "new"), [stale], NOW, actor="stage1")
    assert "f-new" not in store.docs


async def test_write_superseding_with_an_existing_new_id_writes_nothing(store):
    await seed(store, "old")
    await seed(store, "taken")
    old = await resolve(store, "f-old")
    before = copy.deepcopy(store.docs)

    with pytest.raises(AlreadyExists):
        await store.repo.write_superseding(version_of(old, "f-taken", "new"), [old], NOW, actor="stage1")

    assert store.docs == before


async def test_write_superseding_with_nothing_to_replace_is_refused_before_any_io(store, monkeypatch):
    await seed(store, "other")
    before = copy.deepcopy(store.docs)

    def no_transaction():
        raise AssertionError("a transaction was opened")

    monkeypatch.setattr(store.db, "transaction", no_transaction)
    with pytest.raises(ValueError, match="replaces no document"):
        await store.repo.write_superseding(make_fact("new"), [], NOW, actor="stage1")

    assert store.docs == before


@pytest.mark.parametrize("missing", ["vector", "tags_vector", "metadata_vector"])
async def test_write_superseding_refuses_a_new_fact_without_a_vector_before_any_transaction(store, monkeypatch, missing):
    await seed(store, "old")
    old = await resolve(store, "f-old")
    incomplete = version_of(old, "f-new", "new").model_copy(update={missing: None})
    before = copy.deepcopy(store.docs)

    def no_transaction():
        raise AssertionError("a transaction was opened")

    monkeypatch.setattr(store.db, "transaction", no_transaction)
    with pytest.raises(ValueError, match=missing):
        await store.repo.write_superseding(incomplete, [old], NOW, actor="stage1")

    assert store.docs == before


async def test_written_version_doc_passes_check_fact_and_is_found_by_every_reader(store):
    await seed(store, "old", lineage_id="L")
    old = await resolve(store, "f-old")
    await store.repo.write_superseding(version_of(old, "f-new", "new"), [old], NOW, actor="stage1")

    doc = store.docs["f-new"]
    assert check_fact("f-new", doc) == [] and check_fact("f-old", store.docs["f-old"]) == []
    assert all(type(doc[name]) is Vector for name in ("vector", "tags_vector", "metadata_vector"))
    assert doc["context_priority_rank"] == 3 and doc["version"] == 2 and doc["replaces_fact_id"] == "f-old"
    assert doc["lineage_id"] == "L" and doc["superseded_by"] is None
    # Reviewer edit (facts S3, T3; plan §3.8): the closed version carries its "replaced by" entry and still passes the audit.
    assert store.docs["f-old"]["lifecycle_events"] == [{"at": NOW, "from": "current", "to": "superseded",
                                                        "actor": "stage1", "reason": "replaced by f-new"}]
    assert [f.id for f in await store.repo.get_active_facts_ordered(ACC)] == ["f-new"]
    for field, query in (("vector", "new"), ("tags_vector", "t new"), ("metadata_vector", "m new")):
        found = await store.repo.search_facts(embed(query), field, limit=10, user_id=USER, account_id=ACC)
        assert [f.id for f in found] == ["f-new"], field


# ------------------------------------------------------------------------------------------ update_fact_fields

async def test_update_fact_fields_is_field_masked_and_keeps_unknown_stored_fields(store):
    await seed(store, "x")
    await store.repo.facts_col.document("f-x").update({"future_field": 7})
    stored = await resolve(store, "f-x")
    before = copy.deepcopy(store.docs["f-x"])

    await store.repo.update_fact_fields(stored, {
        "state": FactState.ARCHIVED, "is_current": True, "valid_to": NOW, "last_updated": NOW,
    })

    after = store.docs["f-x"]
    assert {k for k in after if after[k] != before[k]} == {"state", "valid_to", "last_updated"}
    assert after["state"] == "archived" and type(after["state"]) is str
    assert after["future_field"] == 7 and set(after) == set(before)     # no superseded_by key was added


async def test_update_fact_fields_refuses_a_content_key_with_its_own_exception(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    before = copy.deepcopy(store.docs)

    with pytest.raises(InPlaceFieldRefused) as caught:
        await store.repo.update_fact_fields(stored, {"last_updated": NOW, "text": "new", "tags": []})

    assert caught.value.fields == ("tags", "text") and not isinstance(caught.value, ValueError)
    assert store.docs == before


async def test_update_fact_fields_conflict_writes_nothing(store):
    await seed(store, "x")
    stale = await resolve(store, "f-x")
    await store.repo.facts_col.document("f-x").update({"text": "edited since the read"})
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactWriteConflict) as caught:
        await store.repo.update_fact_fields(stale, {"last_updated": NOW})

    assert caught.value.fact_id == "f-x" and store.docs == before


async def test_update_fact_fields_on_a_deleted_doc_raises_conflict(store):
    await seed(store, "x")
    stale = await resolve(store, "f-x")
    await store.repo.facts_col.document("f-x").delete()
    with pytest.raises(FactWriteConflict):
        await store.repo.update_fact_fields(stale, {"last_updated": NOW})
    assert "f-x" not in store.docs


# ------------------------------------------------------------------------------------------- error mapping

async def test_read_time_aborted_becomes_conflict(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    store.db.raise_on_txn_read(FACTS, "f-x", Aborted("contention"))
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactWriteConflict) as caught:
        await store.repo.update_fact_fields(stored, {"last_updated": NOW})

    assert caught.value.fact_id == "f-x" and not isinstance(caught.value, ValueError)
    assert store.docs == before


async def test_exhausted_commit_retries_become_conflict(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    gave_up = ValueError("Failed to commit transaction in 5 attempts.")
    gave_up.__cause__ = Aborted("commit aborted")
    store.db.raise_on_txn_read(FACTS, "f-x", gave_up)

    with pytest.raises(FactWriteConflict):
        await store.repo.update_fact_fields(stored, {"last_updated": NOW})


async def test_an_error_that_replaced_an_aborted_is_a_conflict_through_cause_and_context(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    for chain in ("context", "cause", "nested"):
        replacing = RuntimeError("rollback failed")      # e.g. the client's rollback RPC failing after an aborted commit
        if chain == "context":
            replacing.__context__ = Aborted("commit aborted")
        elif chain == "cause":
            replacing.__cause__ = Aborted("commit aborted")
        else:
            middle = ValueError("wrapped")
            middle.__context__ = Aborted("commit aborted")
            replacing.__cause__ = middle
        store.db.raise_on_txn_read(FACTS, "f-x", replacing)

        with pytest.raises(FactWriteConflict) as caught:
            await store.repo.update_fact_fields(stored, {"last_updated": NOW})
        assert caught.value.fact_id == "f-x", chain


async def test_a_chained_error_without_an_aborted_stays_itself(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    replacing = RuntimeError("rollback failed")
    replacing.__context__ = ValueError("unrelated")
    store.db.raise_on_txn_read(FACTS, "f-x", replacing)

    with pytest.raises(RuntimeError, match="rollback failed"):
        await store.repo.update_fact_fields(stored, {"last_updated": NOW})


async def test_a_value_error_not_caused_by_aborted_is_not_a_conflict(store):
    await seed(store, "x")
    stored = await resolve(store, "f-x")
    store.db.raise_on_txn_read(FACTS, "f-x", ValueError("something else"))

    with pytest.raises(ValueError, match="something else"):
        await store.repo.update_fact_fields(stored, {"last_updated": NOW})


# ----------------------------------------------------------------------------------------- resolve_current

async def test_resolve_current_follows_the_lineage_reads_the_whole_doc_and_returns_the_token(store):
    await seed_closed(store, "v1", "f-v2", lineage_id="L")
    await seed_closed(store, "v2", "f-v3", lineage_id="L", version=2)
    await seed(store, "v3", lineage_id="L", version=3)

    stored = await resolve(store, "f-v1")

    assert stored.fact.id == "f-v3" and stored.fact.version == 3
    assert stored.fact.vector == embed("v3") and stored.fact.tags_vector == embed("t v3")
    assert stored.fact.metadata_vector == embed("m v3")
    assert type(stored.fact.vector) is list
    assert stored.update_time == (await store.repo.facts_col.document("f-v3").get()).update_time
    assert (await resolve(store, "f-v3")).fact.id == "f-v3"


async def test_resolve_current_missing_is_none(store):
    assert await resolve(store, "nothing") is None


async def test_resolve_current_foreign_start_is_none_even_when_merged_in_its_account(store, monkeypatch):
    await seed_closed(store, "x", "f-y", account=OTHER, lineage_id="L1")
    await seed(store, "y", account=OTHER, lineage_id="L2")
    import src.adapters.firestore_repo as repo_module

    def never(*args, **kwargs):
        raise AssertionError("forward_step was called for a foreign start document")

    monkeypatch.setattr(repo_module, "forward_step", never)
    assert await resolve(store, "f-x") is None


async def test_resolve_current_stored_cycle_ends_in_the_resolution_error(store):
    await seed_closed(store, "a", "f-b", lineage_id="L")
    await seed_closed(store, "b", "f-a", lineage_id="L", version=2)

    with pytest.raises(FactResolutionError, match="forward hops"):
        await resolve(store, "f-a")


async def test_resolve_current_dangling_superseded_by_is_successor_unknown(store):
    await seed_closed(store, "a", "f-ghost", lineage_id="L")

    with pytest.raises(FactResolutionError, match="successor is unknown"):
        await resolve(store, "f-a")


async def test_resolve_current_names_the_merged_document_of_another_lineage(store):
    await seed_closed(store, "a", "f-m", lineage_id="L1")
    await seed(store, "m", lineage_id="L2")

    with pytest.raises(FactResolutionError) as caught:
        await resolve(store, "f-a")
    assert caught.value.current_id == "f-m"


# ------------------------------------------------------------------------------------------ invalidate_fact

async def test_invalidate_on_a_superseded_id_of_the_same_text_invalidates_the_current_version_and_leaves_one_is_current_per_lineage(store):
    # same text in both versions (e.g. a tags-only version): a newer text is refused, test_fact_invalidate_shown_text.py
    await seed_closed(store, "likes tea", "f-v2", id="f-v1", lineage_id="L")
    await seed(store, "likes tea", id="f-v2", lineage_id="L", version=2)
    old_before = copy.deepcopy(store.docs["f-v1"])

    await store.repo.invalidate_fact("f-v1", ACC)

    assert store.docs["f-v2"]["state"] == "invalidated" and type(store.docs["f-v2"]["state"]) is str
    assert store.docs["f-v2"]["is_current"] is True and store.docs["f-v2"]["valid_to"] is not None
    assert store.docs["f-v1"] == old_before
    assert check_lineages(store.docs.items()) == {}


async def test_invalidate_merged_source_raises_resolution_error_and_writes_nothing(store):
    await seed_closed(store, "a", "f-m", lineage_id="L1")
    await seed(store, "m", lineage_id="L2")
    before = copy.deepcopy(store.docs)

    with pytest.raises(FactResolutionError) as caught:
        await store.repo.invalidate_fact("f-a", ACC)

    assert caught.value.current_id == "f-m" and not isinstance(caught.value, ValueError)
    assert store.docs == before


async def test_invalidate_one_conflict_resolves_again_and_succeeds(store):
    await seed(store, "x")
    store.interfere("f-x", {"text": "edited elsewhere"})

    await store.repo.invalidate_fact("f-x", ACC)

    assert store.docs["f-x"]["text"] == "edited elsewhere"          # the interference happened
    assert store.docs["f-x"]["state"] == "invalidated"


async def test_invalidate_conflict_from_a_concurrent_version_of_the_same_text_lands_on_the_new_version(store):
    # same text in both versions: a concurrent version with another text is refused, test_fact_invalidate_shown_text.py
    await seed(store, "likes tea", id="f-v1", lineage_id="L")
    await seed(store, "likes tea", id="f-v2", lineage_id="L", version=2)
    closure = {"state": "superseded", "is_current": False, "superseded_by": "f-v2", "valid_to": NOW}
    store.interfere("f-v1", closure)    # a version write closes f-v1 between invalidate's resolve and its write

    await store.repo.invalidate_fact("f-v1", ACC)

    assert store.docs["f-v1"]["state"] == "superseded" and store.docs["f-v1"]["is_current"] is False
    assert store.docs["f-v1"]["superseded_by"] == "f-v2" and store.docs["f-v1"]["valid_to"] == NOW
    assert store.docs["f-v2"]["state"] == "invalidated" and store.docs["f-v2"]["is_current"] is True
    assert check_lineages(store.docs.items()) == {}


async def test_invalidate_second_conflict_raises_and_writes_nothing(store):
    await seed(store, "x")
    store.interfere("f-x", {"text": "first edit"})
    store.interfere("f-x", {"text": "second edit"})

    with pytest.raises(FactWriteConflict):
        await store.repo.invalidate_fact("f-x", ACC)

    assert store.docs["f-x"]["text"] == "second edit" and store.docs["f-x"]["state"] == "current"


async def test_invalidate_aborted_is_conflict_not_value_error(store):
    await seed(store, "x")
    store.db.raise_on_txn_read(FACTS, "f-x", Aborted("contention"))
    store.db.raise_on_txn_read(FACTS, "f-x", Aborted("contention"))

    with pytest.raises(FactWriteConflict) as caught:
        await store.repo.invalidate_fact("f-x", ACC)

    assert not isinstance(caught.value, ValueError) and store.docs["f-x"]["state"] == "current"


# --------------------------------------------------------------------------------------------- lineage reads

async def test_get_lineage_is_tenant_filtered_vector_free_and_ordered_by_version(store):
    await seed(store, "v3", lineage_id="L", version=3, age_days=1)
    await seed(store, "v1", lineage_id="L", version=1, age_days=3)
    await seed(store, "v2", lineage_id="L", version=2, age_days=2)
    await seed(store, "foreign", lineage_id="L", account=OTHER)
    await seed(store, "elsewhere", lineage_id="M")

    got = await store.repo.get_lineage(ACC, "L")

    assert [f.id for f in got] == ["f-v1", "f-v2", "f-v3"]
    assert all(f.vector is None and f.tags_vector is None and f.metadata_vector is None for f in got)
    assert all(isinstance(f, FactEntity) and f.account_id == ACC for f in got)
    assert await store.repo.get_lineage(ACC, "none") == []


async def test_latest_by_lineage_skips_superseded_and_returns_the_highest_version(store):
    await seed_closed(store, "v1", "f-v2", lineage_id="L")
    await seed(store, "v2", lineage_id="L", version=2, state=FactState.ARCHIVED)
    await seed_closed(store, "v3", "f-x", lineage_id="L", version=3)
    await seed(store, "foreign", lineage_id="L", account=OTHER, version=9)

    got = await store.repo.get_latest_fact_by_lineage(ACC, "L")

    assert got.id == "f-v2" and type(got.vector) is list
    await seed_closed(store, "only", "f-gone", lineage_id="M")
    assert await store.repo.get_latest_fact_by_lineage(ACC, "M") is None
    assert await store.repo.get_latest_fact_by_lineage(ACC, "none") is None
