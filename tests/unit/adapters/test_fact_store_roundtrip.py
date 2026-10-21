"""Fact write -> stored document -> read round trips through the REAL adapter, write service and repository.

Nothing here is mocked at the port: the Firestore client is an evaluating fake, and assertions are on the raw stored
documents and on what readers return.
"""
import copy
from datetime import datetime, timezone

import pytest

from src.domain.fact_embedding import fact_embedding_texts
from tests.support.fact_store import FactStore, embed
from tests.support.firestore_eval import stored_vector

ACC = "acc-1"
OTHER = "acc-2"


def _meta(**over):
    base = {
        "account_id": ACC, "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family", "mother"], "metadata": {"name": "Maria"},
        "type": "event",
    }
    base.update(over)
    return base


@pytest.fixture
def store():
    return FactStore()


async def _create(store, text="mother is Maria", **over):
    res = await store.adapter.create_fact(text, _meta(**over))
    assert res["status"] == "created", res
    return res["fact_id"]


async def test_create_fact_stores_vectors_for_the_embedding_texts_and_a_real_datetime(store):
    fid = await _create(store, reported_date="2026-03-04T10:00:00Z")
    doc = store.docs[fid]
    # the write service adds the system tag "consolidated" after embedding; the recipe ignores it
    texts = fact_embedding_texts("mother is Maria", ["family", "mother"], {"name": "Maria"})
    assert stored_vector(doc["vector"]) == pytest.approx(embed(texts[0]))
    assert stored_vector(doc["tags_vector"]) == pytest.approx(embed(texts[1]))
    assert stored_vector(doc["metadata_vector"]) == pytest.approx(embed(texts[2]))
    assert isinstance(doc["reported_date"], datetime)
    assert doc["reported_date"].tzinfo is not None
    assert doc["reported_date"] == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
    assert doc["state"] == "current" and doc["is_current"] is True and doc["valid_to"] is None


async def test_update_content_reembeds_and_bumps_version(store):
    fid = await _create(store)
    res = await store.adapter.update_fact(fid, {"content": "lives in Springfield, Ohio"}, ACC)
    assert res["status"] == "updated" and res["version"] == 2
    doc = store.docs[fid]
    assert doc["text"] == "mother is Mariya Doe"
    assert stored_vector(doc["vector"]) == pytest.approx(embed("mother is Mariya Doe"))
    assert doc["version"] == 2
    read = await store.repo.get_fact_by_id(fid)
    assert read is not None and read.text == "mother is Mariya Doe" and read.version == 2


async def test_update_metadata_merges_and_reembeds_metadata_vector(store):
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"metadata": {"city": "Springfield"}}, ACC)
    doc = store.docs[fid]
    assert doc["metadata"] == {"name": "Maria", "city": "Springfield"}
    texts = fact_embedding_texts("mother is Maria", doc["tags"], doc["metadata"])
    assert stored_vector(doc["metadata_vector"]) == pytest.approx(embed(texts[2]))


async def test_state_only_update_keeps_vectors_version_and_reported_date(store):
    fid = await _create(store)
    before = copy.deepcopy(store.docs[fid])
    res = await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    assert res["status"] == "updated"
    doc = store.docs[fid]
    for key in ("vector", "tags_vector", "metadata_vector"):
        assert stored_vector(doc[key]) == stored_vector(before[key])
    assert doc["version"] == before["version"]
    assert doc["reported_date"] == before["reported_date"]
    assert doc["state"] == "archived" and doc["valid_to"] is not None and doc["is_current"] is True


async def test_superseded_update_closes_the_version(store):
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "SUPERSEDED"}, ACC)
    doc = store.docs[fid]
    assert doc["state"] == "superseded" and doc["is_current"] is False and doc["valid_to"] is not None


async def test_repo_invalidate_sets_state_and_valid_to(store):
    fid = await _create(store)
    await store.repo.invalidate_fact(fid, ACC)
    doc = store.docs[fid]
    assert doc["state"] == "invalidated" and doc["valid_to"] is not None and doc["is_current"] is True


async def test_update_with_another_accounts_id_is_not_found_and_changes_nothing(store):
    fid = await _create(store)
    before = copy.deepcopy(store.docs[fid])
    res = await store.adapter.update_fact(fid, {"content": "hijacked", "state": "ARCHIVED"}, OTHER)
    assert res["status"] == "not_found"
    assert store.docs[fid] == before


async def test_invalidate_with_another_accounts_id_is_refused_and_changes_nothing(store):
    fid = await _create(store)
    before = copy.deepcopy(store.docs[fid])
    with pytest.raises(PermissionError):
        await store.repo.invalidate_fact(fid, OTHER)
    assert store.docs[fid] == before


async def test_merge_supersedes_own_sources_only_and_creates_current_fact(store):
    own1 = await _create(store, "likes tea")
    own2 = await _create(store, "likes green tea")
    foreign = await _create(store, "foreign fact", account_id=OTHER)
    foreign_before = copy.deepcopy(store.docs[foreign])
    res = await store.adapter.merge_facts([own1, own2, foreign], "likes green tea daily", _meta())
    assert res["status"] == "merged"
    assert sorted(res["old_fact_ids"]) == sorted([own1, own2])
    for fid in (own1, own2):
        d = store.docs[fid]
        assert d["state"] == "superseded" and d["is_current"] is False and d["valid_to"] is not None
    assert store.docs[foreign] == foreign_before
    new = store.docs[res["new_fact_id"]]
    assert new["state"] == "current" and new["is_current"] is True and new["valid_to"] is None
    assert stored_vector(new["vector"]) == pytest.approx(embed("likes green tea daily"))


async def test_lifecycle_invariants_hold_for_every_stored_document(store):
    a = await _create(store, "fact a")
    b = await _create(store, "fact b")
    c = await _create(store, "fact c")
    d = await _create(store, "fact d")
    e = await _create(store, "fact e")
    await store.adapter.update_fact(a, {"state": "ARCHIVED"}, ACC)
    await store.adapter.update_fact(b, {"state": "SUPERSEDED"}, ACC)
    await store.repo.invalidate_fact(c, ACC)
    await store.adapter.update_fact(d, {"state": "STALE"}, ACC)
    await store.adapter.update_fact(e, {"content": "fact e edited"}, ACC)
    await store.adapter.merge_facts([e], "fact e merged", _meta())
    assert len(store.docs) == 6
    for fid, doc in store.docs.items():
        state = doc["state"]
        if state == "superseded":
            assert doc["is_current"] is False and doc["valid_to"] is not None, fid
        if state in ("archived", "invalidated"):
            assert doc["valid_to"] is not None, fid
        if state in ("current", "stale"):
            assert doc["valid_to"] is None, fid


# --- concurrency, idempotent state, validation order ---------------------------------------------------------

class _CountingSlowEmbedder:
    """Delegates to the hash embedder, sleeps briefly and counts calls."""

    def __init__(self, inner, delay: float = 0.02) -> None:
        self._inner, self._delay, self.calls = inner, delay, 0

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        import asyncio
        self.calls += 1
        await asyncio.sleep(self._delay)
        return await self._inner.get_embeddings_batch(texts, task_type)


def _slow(store):
    emb = _CountingSlowEmbedder(store.embedder)
    store.adapter._embedding = emb
    return emb


async def test_concurrent_content_and_state_updates_of_one_fact_both_survive(store):
    import asyncio
    fid = await _create(store)
    _slow(store)
    r1, r2 = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Mariia"}, ACC),
        store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC),
    )
    assert r1["status"] == "updated" and r2["status"] == "updated"
    doc = store.docs[fid]
    assert doc["text"] == "mother is Valentyna"
    assert stored_vector(doc["vector"]) == pytest.approx(embed("mother is Valentyna"))
    assert doc["state"] == "archived" and doc["valid_to"] is not None
    assert doc["version"] == 2  # bumped exactly once
    assert store.adapter._fact_locks == {}


async def test_merge_concurrent_with_update_of_a_source_leaves_a_consistent_superseded_doc(store):
    import asyncio
    a = await _create(store, "likes tea")
    b = await _create(store, "likes green tea")
    _slow(store)
    upd, merged = await asyncio.gather(
        store.adapter.update_fact(a, {"content": "likes black tea"}, ACC),
        store.adapter.merge_facts([a, b], "likes tea daily", _meta()),
    )
    assert merged["status"] == "merged" and sorted(merged["old_fact_ids"]) == sorted([a, b])
    assert upd["status"] in ("updated", "failed")
    for fid in (a, b):
        d = store.docs[fid]
        assert d["state"] == "superseded" and d["is_current"] is False and d["valid_to"] is not None
    assert store.adapter._fact_locks == {}


async def test_lock_registry_is_empty_after_every_outcome(store):
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "FOO"}, ACC)
    await store.adapter.update_fact("missing", {"content": "x"}, ACC)
    await store.adapter.update_fact(fid, {"content": "y"}, OTHER)
    await store.adapter.merge_facts([fid, "missing"], "z", _meta())
    assert store.adapter._fact_locks == {}


async def test_reapplying_archived_keeps_the_original_valid_to(store):
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    first = store.docs[fid]["valid_to"]
    await store.adapter.update_fact(fid, {"state": "archived"}, ACC)
    assert store.docs[fid]["valid_to"] == first and first is not None


async def test_archived_to_current_revives_and_clears_valid_to(store):
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    await store.adapter.update_fact(fid, {"state": "CURRENT"}, ACC)
    doc = store.docs[fid]
    assert doc["state"] == "current" and doc["valid_to"] is None and doc["is_current"] is True


@pytest.mark.parametrize("bad", [
    {"reported_date": "Oct 2026"},
    {"state": "FOO"},
    {"temporal_class": "sometimes"},
])
async def test_bad_value_fails_before_embedding_and_stores_nothing(store, bad):
    fid = await _create(store)
    emb = _slow(store)
    before = copy.deepcopy(store.docs[fid])
    res = await store.adapter.update_fact(fid, {"content": "changed text", **bad}, ACC)
    assert res["status"] == "failed"
    assert emb.calls == 0
    assert store.docs[fid] == before
