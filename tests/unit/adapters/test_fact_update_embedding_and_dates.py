"""Facts S2 · T4 reviewer: when `update_fact` embeds and what it does to `reported_date`, on the evaluating fake.

These are the behaviours of the retired mock suite `test_fact_management_adapter.py` (it asserted on
`FactRepository.update_fact`, which S2 deletes) that no other fact-store test pinned. The hash embedder returns the same
vectors for the same texts, so "the vectors are kept" cannot tell a copy from a re-embedding: each test here counts the
embedder's calls instead.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

VECTORS = ("vector", "tags_vector", "metadata_vector")


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


class CountingEmbedder:
    def __init__(self, inner):
        self._inner, self.calls = inner, 0

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls += 1
        return await self._inner.get_embeddings_batch(texts, task_type)


async def create(store, **over):
    meta = {"account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
            "context_priority": "medium", "tags": ["family", "mother"], "metadata": {"name": "Valentina"},
            "expiry": EXPIRY_NONE}
    meta.update(over)
    res = await store.adapter.create_fact("mother is Valentina", meta, context=ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


def counting(store):
    embedder = CountingEmbedder(store.embedder)
    store.adapter._embedding = embedder
    return embedder


async def test_the_same_text_again_does_not_embed_and_stays_in_place(store):
    # was test_fact_management_adapter.py::test_same_text_again_does_not_reembed
    fid = await create(store)
    embedder = counting(store)
    res = await store.adapter.update_fact(fid, {"content": "mother is Valentina", "expiry": EXPIRY_NONE}, ACC, context=ctx())
    assert (res["status"], res["fact_id"], res["version"]) == ("updated", fid, 1) and list(store.docs) == [fid]
    assert embedder.calls == 0


async def test_a_non_dict_metadata_is_ignored_and_does_not_embed(store):
    # was test_fact_management_adapter.py::test_non_dict_metadata_is_ignored
    fid = await create(store)
    embedder = counting(store)
    res = await store.adapter.update_fact(fid, {"metadata": "oops", "expiry": EXPIRY_NONE}, ACC, context=ctx())
    assert (res["status"], res["fact_id"], res["version"]) == ("updated", fid, 1) and list(store.docs) == [fid]
    assert store.docs[fid]["metadata"] == {"name": "Valentina"} and embedder.calls == 0


async def test_a_foreign_accounts_update_does_not_embed(store):
    # was test_fact_management_adapter.py::test_foreign_account_update_is_not_found_and_untouched (the "not found,
    # untouched" half is test_fact_management_kit.py::test_update_of_a_missing_fact_or_another_accounts_fact_...)
    fid = await create(store)
    embedder = counting(store)
    res = await store.adapter.update_fact(fid, {"content": "hijack", "expiry": EXPIRY_NONE}, OTHER, context=ctx())
    assert res["status"] == "not_found" and embedder.calls == 0


async def test_a_temporal_class_change_is_a_version_without_an_embedding(store):
    # was test_fact_management_adapter.py::test_temporal_class_change_bumps_version_without_reembedding
    fid = await create(store)
    embedder = counting(store)
    res = await store.adapter.update_fact(fid, {"temporal_class": "PERMANENT", "expiry": EXPIRY_NONE}, ACC, context=ctx())
    assert res["status"] == "updated" and res["version"] == 2 and res["fact_id"] != fid
    assert embedder.calls == 0
    new, old = store.docs[res["fact_id"]], store.docs[fid]
    assert new["temporal_class"] == "permanent"
    assert all(new[field] == old[field] for field in VECTORS)    # the very vectors, copied


async def test_a_state_change_with_an_explicit_reported_date_applies_both_in_place(store):
    # was test_fact_management_adapter.py::test_state_only_update_with_explicit_reported_date_applies_it
    # Reviewer edit (facts S3, T4a): an update never moves reported_date back (D2, S3-D2; plan §3.5), so the fact is
    # created before the sent date, which is then applied with the state in the same in-place write.
    fid = await create(store, reported_date="2019-06-01T00:00:00Z")
    embedder = counting(store)
    res = await store.adapter.update_fact(fid, {"state": "ARCHIVED", "reported_date": "2020-01-02T03:04:05Z"}, ACC, context=ctx())
    assert (res["status"], res["fact_id"], res["version"]) == ("updated", fid, 1) and list(store.docs) == [fid]
    doc = store.docs[fid]
    assert doc["state"] == "archived" and doc["reported_date"] == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert doc["version"] == 1 and embedder.calls == 0


async def test_a_content_change_without_a_date_keeps_reported_date_on_the_new_version(store):
    # was test_fact_management_adapter.py::test_content_change_bumps_version_and_resets_reported_date (the S1 rule,
    # kept by S2). Reviewer edit (facts S3, T2): RFC D2 replaces it; reported_date means "when the owner last stated it",
    # so a content change that sends no date keeps the stored one on the new version (no span here; a conversation pass
    # with a span moves it to max(stored, span end), test_fact_write_context.py).
    fid = await create(store, reported_date=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    old_reported = store.docs[fid]["reported_date"]
    res = await store.adapter.update_fact(fid, {"content": "mother is Valentina P.", "expiry": EXPIRY_NONE}, ACC, context=ctx())
    new = store.docs[res["fact_id"]]
    assert res["version"] == 2 and new["version"] == 2
    assert new["reported_date"] == old_reported
    assert store.docs[fid]["reported_date"] == old_reported
