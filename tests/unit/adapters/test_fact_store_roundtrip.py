"""Fact write -> stored document -> read round trips through the REAL adapter, write service and repository.

Nothing here is mocked at the port: the Firestore client is an evaluating fake, and assertions are on the raw stored
documents and on what readers return.
"""
import copy
from datetime import datetime, timezone

import pytest

from src.domain.entities import FactState
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.fact_lifecycle import CONSOLIDATION_ACCEPTS, lifecycle_fields
from tests.support.fact_store import FactStore, embed
from tests.support.firestore_eval import stored_vector

ACC = "acc-1"
OTHER = "acc-2"


def _meta(**over):
    base = {
        "account_id": ACC, "user_id": "usr-1", "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family", "mother"], "metadata": {"name": "Valentina"},
        "type": "event",
    }
    base.update(over)
    return base


@pytest.fixture
def store():
    return FactStore()


async def _create(store, text="mother is Valentina", **over):
    res = await store.adapter.create_fact(text, _meta(**over))
    assert res["status"] == "created", res
    return res["fact_id"]


async def test_create_fact_stores_vectors_for_the_embedding_texts_and_a_real_datetime(store):
    fid = await _create(store, reported_date="2026-03-04T10:00:00Z")
    doc = store.docs[fid]
    # the write service adds the system tag "consolidated" after embedding; the recipe ignores it
    texts = fact_embedding_texts("mother is Valentina", ["family", "mother"], {"name": "Valentina"})
    assert stored_vector(doc["vector"]) == pytest.approx(embed(texts[0]))
    assert stored_vector(doc["tags_vector"]) == pytest.approx(embed(texts[1]))
    assert stored_vector(doc["metadata_vector"]) == pytest.approx(embed(texts[2]))
    assert isinstance(doc["reported_date"], datetime)
    assert doc["reported_date"].tzinfo is not None
    assert doc["reported_date"] == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
    assert doc["state"] == "current" and doc["is_current"] is True and doc["valid_to"] is None


async def test_update_content_reembeds_and_bumps_version(store):
    # Reviewer edit (facts S2, D8): a content change is a new version document; it is read through the returned id.
    fid = await _create(store)
    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna Ivanivna"}, ACC)
    assert res["status"] == "updated" and res["version"] == 2
    new_id = res["fact_id"]
    assert new_id != fid
    doc = store.docs[new_id]
    assert doc["text"] == "mother is Valentyna Ivanivna"
    assert stored_vector(doc["vector"]) == pytest.approx(embed("mother is Valentyna Ivanivna"))
    assert doc["version"] == 2
    read = await store.repo.get_fact_by_id(new_id)
    assert read is not None and read.text == "mother is Valentyna Ivanivna" and read.version == 2
    old = await store.repo.get_fact_by_id(fid)
    assert old is not None and old.text == "mother is Valentina" and old.version == 1 and old.superseded_by == new_id


async def test_update_metadata_merges_and_reembeds_metadata_vector(store):
    # Reviewer edit (facts S2, D8): a metadata change is a new version document, read through the returned id.
    fid = await _create(store)
    res = await store.adapter.update_fact(fid, {"metadata": {"city": "Kyiv"}}, ACC)
    assert res["fact_id"] != fid and store.docs[fid]["metadata"] == {"name": "Valentina"}
    doc = store.docs[res["fact_id"]]
    assert doc["metadata"] == {"name": "Valentina", "city": "Kyiv"}
    texts = fact_embedding_texts("mother is Valentina", doc["tags"], doc["metadata"])
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
    # Reviewer edit (facts S2, D8): SUPERSEDED alone is refused and writes nothing; with superseded_by it closes the fact.
    fid = await _create(store)
    successor = await _create(store, "mother is Valentyna")
    before = copy.deepcopy(store.docs)
    refused = await store.adapter.update_fact(fid, {"state": "SUPERSEDED"}, ACC)
    assert refused["status"] == "failed" and store.docs == before
    await store.adapter.update_fact(fid, {"state": "SUPERSEDED", "superseded_by": successor}, ACC)
    doc = store.docs[fid]
    assert doc["state"] == "superseded" and doc["is_current"] is False and doc["valid_to"] is not None
    assert doc["superseded_by"] == successor


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
    # Reviewer edit (facts S2, D8): b is superseded in favour of d (SUPERSEDED alone is refused); e's edit is a version
    # document e2, and the merge of e resolves forward to e2. Seven documents; every write must succeed.
    results = [
        await store.adapter.update_fact(a, {"state": "ARCHIVED"}, ACC),
        await store.adapter.update_fact(b, {"state": "SUPERSEDED", "superseded_by": d}, ACC),
    ]
    await store.repo.invalidate_fact(c, ACC)
    # Reviewer edit (facts S3, T2): only the system marks a fact stale (D1, the model's STALE is refused), so d is
    # staled the way the system writes in place, through the repository, and the stale invariant below keeps its document.
    stored_d = await store.repo.resolve_current(d, ACC, CONSOLIDATION_ACCEPTS)
    await store.repo.update_fact_fields(
        stored_d, {**lifecycle_fields(FactState.STALE, datetime.now(timezone.utc)), "last_updated": datetime.now(timezone.utc)})
    edited = await store.adapter.update_fact(e, {"content": "fact e edited"}, ACC)
    merged = await store.adapter.merge_facts([e], "fact e merged", _meta())
    assert all(r["status"] == "updated" for r in [*results, edited])
    assert merged["status"] == "merged" and merged["old_fact_ids"] == [edited["fact_id"]]
    assert len(store.docs) == 7 and store.docs[d]["state"] == "stale"
    for fid, doc in store.docs.items():
        state = doc["state"]
        if state == "superseded":
            assert doc["is_current"] is False and doc["valid_to"] is not None, fid
            assert doc["superseded_by"] in store.docs, fid
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


async def test_concurrent_content_and_state_updates_of_one_fact_archive_wins_and_content_fails(store):
    # Reviewer edit (facts S2, D9, plan R2-M4): no lock. Interleaving source: the slow embedder on the adapter. The state
    # write needs no embedding and commits first; the content call conflicts, re-resolves to an archived fact and fails.
    import asyncio
    fid = await _create(store)
    emb = _slow(store)
    r1, r2 = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC),
        store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC),
    )
    assert r2["status"] == "updated"
    assert r1["status"] == "failed" and r1["message"] == f"fact {fid} is archived"
    assert emb.calls == 1  # the content call embedded before it lost the race
    doc = store.docs[fid]
    assert doc["text"] == "mother is Valentina" and doc["version"] == 1
    assert doc["state"] == "archived" and doc["valid_to"] is not None
    assert list(store.docs) == [fid]  # no version document, so one is_current per lineage


async def test_merge_concurrent_with_update_of_a_source_leaves_a_consistent_superseded_doc(store):
    # Reviewer edit (facts S2, plan N2): the lock assertion is gone (D9). The merge commits first; the update conflicts,
    # re-resolves into the merged fact's lineage and fails naming it.
    # Reviewer edit 2 (F1 follow-up): the interleaving was a timing race (a 20 ms sleep in the update's embedder against
    # the merge's whole path, which yields inside the fake) and flipped under coverage. Interleaving source now: the
    # update's embedder waits on an event that only the merge's commit sets; the commit order is recorded and asserted.
    import asyncio
    a = await _create(store, "likes tea")
    b = await _create(store, "likes green tea")
    merge_committed = asyncio.Event()
    emb = _GatedEmbedder(store.embedder, merge_committed)
    store.adapter._embedding = emb
    commits = _record_commits(store.repo, on_ok=lambda replaced: len(replaced) == 2 and merge_committed.set())
    upd, merged = await asyncio.gather(
        store.adapter.update_fact(a, {"content": "likes black tea"}, ACC),
        store.adapter.merge_facts([a, b], "likes tea daily", _meta()),
    )
    assert commits == [("ok", [a, b]), ("conflict", [a])]  # the merge committed, then the update's write lost
    assert merged["status"] == "merged" and merged["old_fact_ids"] == [a, b]
    assert upd["status"] == "failed" and upd["message"] == f"fact {a} was merged into {merged['new_fact_id']}; search again"
    assert emb.calls == 1  # the update embedded once, before it lost the race
    for fid in (a, b):
        d = store.docs[fid]
        assert d["state"] == "superseded" and d["is_current"] is False and d["valid_to"] is not None
        assert d["superseded_by"] == merged["new_fact_id"]
    assert len(store.docs) == 3 and store.docs[a]["text"] == "likes tea"


class _GatedEmbedder:
    """Delegates to the hash embedder once `gate` is set (5 s guard against a hang); counts calls."""

    def __init__(self, inner, gate) -> None:
        self._inner, self._gate, self.calls = inner, gate, 0

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        import asyncio
        self.calls += 1
        await asyncio.wait_for(self._gate.wait(), timeout=5)
        return await self._inner.get_embeddings_batch(texts, task_type)


def _record_commits(repo, on_ok):
    """Wraps `write_superseding`: records ("ok" | "conflict", replaced ids) per call, calls `on_ok(replaced)` after a commit."""
    from src.domain.exceptions import FactWriteConflict
    original, commits = repo.write_superseding, []

    async def recording(new_fact, replaced, now):
        ids = [stored.fact.id for stored in replaced]
        try:
            await original(new_fact, replaced, now)
        except FactWriteConflict:
            commits.append(("conflict", ids))
            raise
        commits.append(("ok", ids))
        on_ok(replaced)

    repo.write_superseding = recording
    return commits


async def test_reapplying_archived_is_refused_and_keeps_the_original_valid_to(store):
    # Reviewer edit (facts S2, D8, plan R1-S2): the consolidator accepts only a current end, so the second call is refused
    # and was green for that reason; the refusal is now asserted. "Re-applying the stored state does not move valid_to"
    # is pinned on the plan: tests/unit/domain/test_fact_update_plan.py::test_reapplying_the_stored_state_does_not_move_valid_to.
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    first = store.docs[fid]["valid_to"]
    before = copy.deepcopy(store.docs)
    res = await store.adapter.update_fact(fid, {"state": "archived"}, ACC)
    assert res["status"] == "failed" and res["message"] == f"fact {fid} is archived"
    assert store.docs[fid]["valid_to"] == first and first is not None and store.docs == before


async def test_archived_to_current_is_refused_for_the_consolidator(store):
    # Reviewer edit (facts S2, D8): the consolidator accepts only a current end; reviving an archived fact is the S4
    # review pass's job (its accepted set adds STALE). The archived document is left as it was.
    fid = await _create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    before = copy.deepcopy(store.docs)
    res = await store.adapter.update_fact(fid, {"state": "CURRENT"}, ACC)
    assert res["status"] == "failed" and res["message"] == f"fact {fid} is archived"
    doc = store.docs[fid]
    assert doc["state"] == "archived" and doc["valid_to"] is not None and store.docs == before


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
