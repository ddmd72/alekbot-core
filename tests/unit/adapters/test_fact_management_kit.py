"""FactManagementPort kit (P4 · T3): the docstrings of `src/ports/fact_management_port.py` checked against the real
`FactManagementAdapter` (with the real `FactWriteService`, `FirestoreFactRepository` and, for search, the real
`SearchEnrichmentService`) on the evaluating Firestore fake, with the index refusal ON. Assertions are on the raw stored
documents and on the returned dicts. `discard_candidate` has no caller in `src/` and is not tested.

`store` is the one fixture that selects the backend.
"""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from google.cloud.firestore_v1.vector import Vector

from src.adapters.fact_management_adapter import FactManagementAdapter
from src.domain.entities import FactState
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.request_context import RequestContext
from src.services.search_enrichment_service import SearchEnrichmentService
from tests.support.fact_kit import ACC, OTHER, USER, as_legacy, make_fact, put_raw
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed
from tests.support.firestore_eval import stored_vector


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family", "mother"], "metadata": {"name": "Maria"}, "type": "event",
    }
    base.update(over)
    return base


async def create(store, text="mother is Maria", **over):
    res = await store.adapter.create_fact(text, meta(**over))
    assert res["status"] == "created", res
    return res["fact_id"]


def snapshot(store):
    return copy.deepcopy(store.docs)


class FailingEmbedder:
    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("embedding service down")


# ----------------------------------------------------------------------------------------------- create_fact

async def test_create_fact_stores_one_document_with_the_given_attributes(store):
    other = await create(store, "foreign fact", account_id=OTHER, user_id="usr-2")
    other_before = copy.deepcopy(store.docs[other])
    res = await store.adapter.create_fact("mother is Maria", meta(
        tags=["family"], metadata={"name": "Maria"}, context="said at lunch", ttl_days=30, type="STATE",
        domain="BIOGRAPHICAL", temporal_class="STABLE", context_priority="HIGH", state="STALE",
        reported_date="2026-03-04T10:00:00Z"))
    assert set(res) == {"fact_id", "status", "message"} and res["status"] == "created"
    json.dumps(res)
    doc = store.docs[res["fact_id"]]
    assert len(store.docs) == 2 and store.docs[other] == other_before
    assert (doc["account_id"], doc["created_by_user_id"], doc["text"]) == (ACC, USER, "mother is Maria")
    assert doc["tags"] == ["family", "consolidated"] and doc["metadata"] == {"name": "Maria"}
    assert (doc["domain"], doc["temporal_class"], doc["context_priority"], doc["state"], doc["type"]) == (
        "biographical", "stable", "high", "stale", "state")
    assert (doc["context"], doc["ttl_days"], doc["version"]) == ("said at lunch", 30, 1)
    assert doc["reported_date"] == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
    assert doc["lineage_id"] and doc["id"] == res["fact_id"]
    texts = fact_embedding_texts("mother is Maria", ["family"], {"name": "Maria"})
    for field, text in zip(("vector", "tags_vector", "metadata_vector"), texts):
        assert stored_vector(doc[field]) == pytest.approx(embed(text))


async def test_create_fact_defaults_type_event_state_current_and_reported_date_now(store):
    started = datetime.now(timezone.utc)
    required = {k: v for k, v in meta().items() if k in ("account_id", "user_id", "domain", "temporal_class",
                                                          "context_priority")}
    res = await store.adapter.create_fact("mother is Maria", required)
    assert res["status"] == "created"
    doc = store.docs[res["fact_id"]]
    assert doc["type"] == "event" and doc["state"] == "current"
    assert started <= doc["reported_date"] <= datetime.now(timezone.utc)
    assert doc["tags"] == ["consolidated"] and doc["metadata"] == {}


async def test_create_fact_with_an_unknown_type_is_an_event(store):
    fid = await create(store, type="gadget")
    assert store.docs[fid]["type"] == "event"


@pytest.mark.parametrize("state", ["current", "archived", "invalidated"])
async def test_create_fact_is_current_with_no_valid_to_whatever_the_state(store, state):
    fid = await create(store, state=state)
    doc = store.docs[fid]
    assert doc["state"] == state and doc["is_current"] is True and doc["valid_to"] is None


async def test_create_fact_refuses_state_superseded_and_writes_nothing(store):
    # Reviewer edit (facts S2, D-2): was the [superseded] case above; a fact cannot be born superseded without a successor.
    res = await store.adapter.create_fact("mother is Maria", meta(state="superseded"))
    assert res["status"] == "failed" and res["fact_id"] is None and "SUPERSEDED" in res["message"]
    assert store.docs == {}


async def test_create_fact_twice_creates_two_facts_with_new_lineages(store):
    first = await create(store)
    second = await create(store)
    assert first != second and len(store.docs) == 2
    assert store.docs[first]["lineage_id"] != store.docs[second]["lineage_id"]


@pytest.mark.parametrize("key", ["account_id", "user_id", "domain", "temporal_class", "context_priority"])
async def test_create_fact_without_a_required_key_fails_and_writes_nothing(store, key):
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    before = snapshot(store)
    bad = meta()
    del bad[key]
    res = await store.adapter.create_fact("mother is Maria", bad)
    assert res["status"] == "failed" and res["fact_id"] is None and key in res["message"]
    assert store.docs == before


@pytest.mark.parametrize("bad", [{"domain": "astrology"}, {"temporal_class": "sometimes"}, {"state": "zombie"},
                                 {"context_priority": "urgent"}])
async def test_create_fact_with_an_invalid_enum_value_fails_and_writes_nothing(store, bad):
    res = await store.adapter.create_fact("mother is Maria", meta(**bad))
    assert res["status"] == "failed" and res["fact_id"] is None
    assert store.docs == {}


async def test_create_fact_with_a_required_key_set_to_none_passes_the_check(store):
    res = await store.adapter.create_fact("mother is Maria", meta(domain=None))
    assert res["status"] == "created" and store.docs[res["fact_id"]]["domain"] is None


async def test_create_fact_never_raises_on_an_embedding_failure(store):
    store.write._embedding = FailingEmbedder()
    res = await store.adapter.create_fact("mother is Maria", meta())
    assert res["status"] == "failed" and res["fact_id"] is None and store.docs == {}


# ----------------------------------------------------------------------------------------------- update_fact

def _without(doc, *keys):
    return {k: v for k, v in doc.items() if k not in keys}


def _plain(doc):
    return {k: (stored_vector(v) if isinstance(v, Vector) else v) for k, v in doc.items()}


async def test_update_returns_the_result_dict_and_json(store):
    fid = await create(store)
    res = await store.adapter.update_fact(fid, {"content": "mother is Mariia"}, ACC)
    assert set(res) == {"fact_id", "status", "version", "message"} and res["fact_id"] == fid
    json.dumps(res)


async def test_update_tags_replaces_them_whole_and_recomputes_all_three_vectors(store):
    fid = await create(store)
    await store.adapter.update_fact(fid, {"tags": ["relatives"]}, ACC)
    doc = store.docs[fid]
    assert doc["tags"] == ["relatives"] and doc["version"] == 2
    for field, text in zip(("vector", "tags_vector", "metadata_vector"),
                           fact_embedding_texts("mother is Maria", ["relatives"], {"name": "Maria"})):
        assert stored_vector(doc[field]) == pytest.approx(embed(text))


async def test_update_metadata_overwrites_a_shared_key_and_keeps_the_others(store):
    fid = await create(store, metadata={"name": "Maria", "city": "Springfield"})
    await store.adapter.update_fact(fid, {"metadata": {"name": "Masha"}}, ACC)
    assert store.docs[fid]["metadata"] == {"name": "Valya", "city": "Kyiv"}


async def test_update_temporal_class_is_case_insensitive_bumps_version_and_keeps_vectors(store):
    fid = await create(store)
    before = _plain(store.docs[fid])
    await store.adapter.update_fact(fid, {"temporal_class": "DYNAMIC"}, ACC)
    doc = _plain(store.docs[fid])
    assert doc["temporal_class"] == "dynamic" and doc["version"] == 2
    for field in ("vector", "tags_vector", "metadata_vector"):
        assert doc[field] == before[field]
    assert doc["reported_date"] > before["reported_date"]


async def test_update_with_the_stored_values_changes_no_version_or_reported_date(store):
    fid = await create(store)
    before = _plain(store.docs[fid])
    res = await store.adapter.update_fact(
        fid, {"content": "mother is Maria", "tags": ["family", "mother", "consolidated"],
              "metadata": {"name": "Maria"}, "temporal_class": "STABLE"}, ACC)
    assert res["status"] == "updated" and res["version"] == 1
    assert _without(_plain(store.docs[fid]), "last_updated") == _without(before, "last_updated")


async def test_update_ignores_other_keys_and_keeps_absent_ones(store):
    fid = await create(store)
    before = _plain(store.docs[fid])
    res = await store.adapter.update_fact(fid, {"domain": "work", "foo": "bar", "context_priority": "low"}, ACC)
    assert res["status"] == "updated"
    after = _plain(store.docs[fid])
    assert {k for k in after if after[k] != before[k]} == {"last_updated"}


async def test_update_sets_last_updated_on_every_update(store):
    fid = await create(store)
    before = store.docs[fid]["last_updated"]
    res = await store.adapter.update_fact(fid, {}, ACC)
    assert res["status"] == "updated" and store.docs[fid]["last_updated"] > before


async def test_update_reported_date_is_taken_from_an_iso_string_or_a_datetime(store):
    fid = await create(store)
    await store.adapter.update_fact(fid, {"content": "edited", "reported_date": "2020-01-02T03:04:05Z"}, ACC)
    assert store.docs[fid]["reported_date"] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    given = datetime(2021, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    await store.adapter.update_fact(fid, {"content": "edited again", "reported_date": given}, ACC)
    assert store.docs[fid]["reported_date"] == given and store.docs[fid]["version"] == 3


@pytest.mark.parametrize("state,is_current,closed", [
    ("CURRENT", True, False), ("STALE", True, False), ("ARCHIVED", True, True), ("INVALIDATED", True, True),
    ("SUPERSEDED", False, True)])
async def test_update_state_applies_the_lifecycle_fields(store, state, is_current, closed):
    fid = await create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    await store.adapter.update_fact(fid, {"state": state}, ACC)
    doc = store.docs[fid]
    assert doc["state"] == state.lower() and doc["is_current"] is is_current and (doc["valid_to"] is not None) is closed


async def test_repeating_an_update_changes_only_last_updated(store):
    fid = await create(store)
    await store.adapter.update_fact(fid, {"content": "mother is Valentyna", "tags": ["family"]}, ACC)
    first = _plain(store.docs[fid])
    await store.adapter.update_fact(fid, {"content": "mother is Valentyna", "tags": ["family"]}, ACC)
    second = _plain(store.docs[fid])
    assert {k for k in second if second[k] != first[k]} == {"last_updated"}


async def test_update_of_a_missing_fact_or_another_accounts_fact_is_not_found_and_writes_nothing(store):
    fid = await create(store)
    await create(store, "foreign fact", account_id=OTHER)
    before = snapshot(store)
    for target, account in (("missing", ACC), (fid, OTHER)):
        res = await store.adapter.update_fact(target, {"content": "hijack", "state": "ARCHIVED"}, account)
        assert res == {"fact_id": target, "status": "not_found", "message": "Fact not found"}
    assert store.docs == before


async def test_update_of_a_legacy_owner_id_document_belongs_to_the_owner(store):
    put_raw(store, "old", "legacy text", **as_legacy())
    before = snapshot(store)
    assert (await store.adapter.update_fact("old", {"content": "edited"}, OTHER))["status"] == "not_found"
    assert store.docs == before
    assert (await store.adapter.update_fact("old", {"content": "edited"}, ACC))["status"] == "updated"
    doc = store.docs["old"]
    assert (doc["account_id"], doc["created_by_user_id"], doc["text"]) == (ACC, ACC, "edited") and "owner_id" not in doc


async def test_update_with_an_embedding_failure_fails_and_writes_nothing(store):
    fid = await create(store)
    before = snapshot(store)
    store.adapter._embedding = FailingEmbedder()
    res = await store.adapter.update_fact(fid, {"content": "changed", "state": "ARCHIVED"}, ACC)
    assert res["status"] == "failed" and res["fact_id"] == fid and "message" in res
    assert store.docs == before


# ----------------------------------------------------------------------------------------------- merge_facts

async def test_merge_returns_the_result_dict_and_creates_one_fact_with_a_new_lineage(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    lineages = {store.docs[a]["lineage_id"], store.docs[b]["lineage_id"]}
    res = await store.adapter.merge_facts([b, a], "likes green tea daily", meta(tags=["drinks"], metadata={"k": "v"}))
    assert set(res) == {"new_fact_id", "old_fact_ids", "old_facts_state", "status", "message"}
    assert (res["status"], res["old_fact_ids"], res["old_facts_state"]) == ("merged", [b, a], "SUPERSEDED")
    json.dumps(res)
    new = store.docs[res["new_fact_id"]]
    assert len(store.docs) == 3 and new["lineage_id"] not in lineages
    assert (new["account_id"], new["created_by_user_id"], new["text"]) == (ACC, USER, "likes green tea daily")
    assert new["tags"] == ["drinks", "consolidated"] and new["metadata"] == {"k": "v"}
    assert (new["domain"], new["state"], new["is_current"], new["valid_to"]) == ("biographical", "current", True, None)
    for field, text in zip(("vector", "tags_vector", "metadata_vector"),
                           fact_embedding_texts("likes green tea daily", ["drinks"], {"k": "v"})):
        assert stored_vector(new[field]) == pytest.approx(embed(text))


async def test_merge_skips_missing_and_foreign_sources_without_reporting_them(store):
    own = await create(store, "likes tea")
    foreign = await create(store, "foreign fact", account_id=OTHER)
    foreign_before = copy.deepcopy(store.docs[foreign])
    res = await store.adapter.merge_facts(["missing", foreign, own], "likes tea daily", meta())
    assert res["status"] == "merged" and res["old_fact_ids"] == [own]
    assert store.docs[foreign] == foreign_before


async def test_merge_without_an_eligible_source_fails_and_writes_nothing(store):
    foreign = await create(store, "foreign fact", account_id=OTHER)
    before = snapshot(store)
    for sources in ([], ["missing"], [foreign]):
        res = await store.adapter.merge_facts(sources, "merged", meta())
        assert res == {"new_fact_id": None, "old_fact_ids": [], "old_facts_state": "FAILED", "status": "failed",
                       "message": "Failed to supersede any facts"}
    assert store.docs == before


async def test_merge_without_an_account_in_the_metadata_fails_and_writes_nothing(store):
    own = await create(store, "likes tea")
    before = snapshot(store)
    bad = meta()
    del bad["account_id"]
    res = await store.adapter.merge_facts([own], "merged", bad)
    assert res["status"] == "failed" and store.docs == before


async def test_merge_never_raises_and_a_failed_creation_is_a_failed_result(store):
    a = await create(store, "likes tea")
    store.write._embedding = FailingEmbedder()
    res = await store.adapter.merge_facts([a], "likes tea daily", meta())
    assert res["status"] == "failed" and res["new_fact_id"] is None and isinstance(res["message"], str)
    json.dumps(res)


async def test_merge_reports_only_the_sources_it_superseded(store, monkeypatch):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    b_before = copy.deepcopy(store.docs[b])
    real_update = store.repo.update_fact

    async def update_unless_b(fact):
        if fact.id == b:
            raise RuntimeError("backend down")
        await real_update(fact)

    monkeypatch.setattr(store.repo, "update_fact", update_unless_b)
    res = await store.adapter.merge_facts([a, b], "likes tea daily", meta())
    assert res["status"] == "merged" and res["old_fact_ids"] == [a]
    assert store.docs[b] == b_before and store.docs[a]["state"] == "superseded"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: merge_facts supersedes the sources before the merged fact is created, so a failed creation leaves "
    "them superseded — src/adapters/fact_management_adapter.py:376-430 (facts S2; P4 T2 arbiter defect 1)"))
async def test_a_failed_merge_leaves_every_source_unchanged(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    before = snapshot(store)
    store.write._embedding = FailingEmbedder()
    res = await store.adapter.merge_facts([a, b], "likes green tea daily", meta())
    assert res["status"] == "failed"
    assert store.docs == before


async def test_merge_needs_only_account_and_user_in_the_metadata(store):
    a = await create(store, "likes tea")
    res = await store.adapter.merge_facts([a], "likes tea daily", {"account_id": ACC, "user_id": USER})
    assert res["status"] == "merged"
    new = store.docs[res["new_fact_id"]]
    assert new["domain"] is None and new["temporal_class"] is None and new["context_priority"] == "medium"


async def test_merge_twice_supersedes_again_and_creates_another_merged_fact(store):
    a = await create(store, "likes tea")
    first = await store.adapter.merge_facts([a], "likes tea daily", meta())
    valid_to = store.docs[a]["valid_to"]
    second = await store.adapter.merge_facts([a], "likes tea daily", meta())
    assert second["status"] == "merged" and second["old_fact_ids"] == [a]
    assert second["new_fact_id"] != first["new_fact_id"] and len(store.docs) == 3
    assert store.docs[a]["state"] == "superseded" and store.docs[a]["valid_to"] > valid_to


# --------------------------------------------------------------------------------------- search_existing_facts

def _searching(store):
    search = SearchEnrichmentService(store.repo, store.embedder, 7, 10, 10, 20)
    return FactManagementAdapter(store.repo, store.embedder, store.write, search)


async def test_search_returns_the_current_facts_of_the_context_account_in_any_domain(store):
    own = [await create(store, "mother is Maria"), await create(store, "owns a Skoda", domain="possession"),
           await create(store, "has a cold", domain="health")]
    await create(store, "mother was Maria", state="archived")
    await create(store, "foreign mother", account_id=OTHER)
    with RequestContext(user_id=USER, account_id=ACC):
        got = await _searching(store).search_existing_facts(["family"], "mother")
    assert sorted(r["fact_id"] for r in got) == sorted(own)


async def test_search_account_is_the_context_account_else_the_user_id(store):
    mine = await create(store, "mine")
    theirs = await create(store, "theirs", account_id=OTHER)
    adapter = _searching(store)
    with RequestContext(user_id=OTHER, account_id=ACC):
        assert [r["fact_id"] for r in await adapter.search_existing_facts(["x"], "mine")] == [mine]
    with RequestContext(user_id=OTHER):
        assert [r["fact_id"] for r in await adapter.search_existing_facts(["x"], "theirs")] == [theirs]


async def test_search_result_dicts_carry_the_listed_fields_and_serialise(store):
    fid = await create(store, "mother is Maria", reported_date="2026-03-04T10:00:00+00:00")
    with RequestContext(user_id=USER, account_id=ACC):
        (got,) = await _searching(store).search_existing_facts(["family"], "mother is Maria")
    assert set(got) == {"fact_id", "content", "similarity", "source", "type", "domain", "temporal_class", "state",
                        "context_priority", "tags", "metadata", "reported_date", "version"}
    assert (got["fact_id"], got["content"], got["type"], got["domain"], got["temporal_class"]) == (
        fid, "mother is Maria", "event", "biographical", "stable")
    assert (got["state"], got["context_priority"], got["version"]) == ("current", "medium", 1)
    assert got["tags"] == ["family", "mother", "consolidated"] and got["metadata"] == {"name": "Maria"}
    assert got["reported_date"] == "2026-03-04T10:00:00+00:00" and got["similarity"] is None
    assert isinstance(got["source"], str) and got["source"]
    json.dumps(got)


async def test_search_ranks_the_best_match_first(store):
    await create(store, "mother is Maria")
    best = await create(store, "owns a Skoda Fabia 2015", tags=["car"], metadata={"make": "Skoda"})
    await create(store, "likes green tea", tags=["drinks"], metadata={})
    with RequestContext(user_id=USER, account_id=ACC):
        got = await _searching(store).search_existing_facts(["car"], "owns a Skoda Fabia")
    assert got[0]["fact_id"] == best


async def test_search_returns_at_most_limit_results_each_fact_once(store):
    for i in range(5):
        await create(store, f"fact number {i}")
    adapter = _searching(store)
    with RequestContext(user_id=USER, account_id=ACC):
        three = await adapter.search_existing_facts(["fact"], "fact number 1", limit=3)
        everything = await adapter.search_existing_facts(["fact"], "fact number 1")
    assert len(three) == 3 and len({r["fact_id"] for r in three}) == 3
    assert len(everything) == 5 and len({r["fact_id"] for r in everything}) == 5


async def test_search_keeps_facts_with_the_same_content_but_different_ids(store):
    first = await create(store, "likes green tea")
    second = await create(store, "likes green tea")
    with RequestContext(user_id=USER, account_id=ACC):
        got = await _searching(store).search_existing_facts(["tea"], "likes green tea")
    assert sorted(r["fact_id"] for r in got) == sorted([first, second])


async def test_search_alternative_query_defaults_to_the_primary(store):
    names = ("alpha", "beta", "gamma", "delta", "epsilon")
    for name in names:
        await create(store, f"{name} fact", tags=[name], metadata={})
    adapter = _searching(store)
    with RequestContext(user_id=USER, account_id=ACC):
        plain_ = await adapter.search_existing_facts(["x"], "alpha fact", limit=2)
        explicit = await adapter.search_existing_facts(["x"], "alpha fact", alternative_query="alpha fact", limit=2)
        assert plain_ == explicit
        others = [await adapter.search_existing_facts(["x"], "alpha fact", alternative_query=f"{n} fact", limit=2)
                  for n in names[1:]]
    assert any(other != plain_ for other in others)


async def test_search_failure_or_no_account_returns_an_empty_list(store):
    await create(store, "likes green tea")
    adapter = _searching(store)
    assert await adapter.search_existing_facts(["tea"], "likes green tea") == []
    store.db.collection(FACTS).fail = RuntimeError("backend down")
    with RequestContext(user_id=USER, account_id=ACC):
        assert await adapter.search_existing_facts(["tea"], "likes green tea") == []
