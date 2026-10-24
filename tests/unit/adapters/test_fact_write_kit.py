"""FactWritePort kit (P4 · T3): the docstring of `src/ports/fact_write_port.py` checked against the real `FactWriteService`
over the real `FirestoreFactRepository` on the evaluating Firestore fake, with the index refusal ON. Assertions are on the
raw stored documents and on the returned tuple.

`store` is the one fixture that selects the backend.
"""
import copy
import math
from datetime import datetime, timezone

import pytest
from google.cloud.firestore_v1.vector import Vector

from src.domain.entities import FactState
from src.domain.fact_embedding import fact_embedding_texts
from src.ports.embedding_service import EmbeddingService
from tests.support.fact_kit import ACC, OTHER, USER, make_fact, put_raw
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed
from tests.support.firestore_eval import stored_vector


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def add(store, facts, skip_deduplication=True):
    return await store.write.add_facts_batch(ACC, USER, facts, skip_deduplication=skip_deduplication)


def texts_of(store, ids):
    return [store.docs[i]["text"] for i in ids]


class ControlledEmbedder(EmbeddingService):
    """Maps a text to a chosen vector (else the hash vector) and counts the calls."""

    def __init__(self, vectors=None, fail_on=None):
        self.vectors, self.fail_on, self.calls = vectors or {}, fail_on, 0

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        return self.vectors.get(text, embed(text))

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls += 1
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("embedding service down")
        return [self.vectors.get(t, embed(t)) for t in texts]


def _near(similarity):
    return [similarity, math.sqrt(1 - similarity ** 2), 0.0]


# ------------------------------------------------------------------------------------- what each fact becomes

async def test_each_fact_gets_a_new_id_a_new_lineage_the_account_the_user_and_three_vectors(store):
    saved, skipped, ids = await add(store, [
        {"text": "mother is Maria", "tags": ["family"], "metadata": {"name": "Maria"}},
        {"content": "owns a Skoda", "tags": [], "type": "state"},
    ])
    assert (saved, skipped) == (2, 0) and len(ids) == 2 and len(store.docs) == 2
    first, second = (store.docs[i] for i in ids)
    assert first["id"] == ids[0] and first["lineage_id"] != second["lineage_id"] and first["lineage_id"]
    for doc in (first, second):
        assert (doc["account_id"], doc["created_by_user_id"], doc["is_current"], doc["version"]) == (ACC, USER, True, 1)
        assert all(isinstance(doc[k], Vector) for k in ("vector", "tags_vector", "metadata_vector"))
    for field, text in zip(("vector", "tags_vector", "metadata_vector"),
                           fact_embedding_texts("mother is Maria", ["family"], {"name": "Maria"})):
        assert stored_vector(first[field]) == pytest.approx(embed(text))
    assert second["metadata"] == {}


async def test_content_wins_over_text(store):
    _, _, ids = await add(store, [{"text": "from text", "content": "from content"}])
    assert store.docs[ids[0]]["text"] == "from content"


async def test_tags_gain_consolidated_and_principles_gain_anchor_once(store):
    own = {"text": "be calm", "tags": ["mindset"], "type": "principle"}
    already = {"text": "stay calm", "tags": ["consolidated", "anchor"], "type": "principle"}
    plain_ = {"text": "likes tea"}
    _, _, ids = await add(store, [own, already, plain_])
    assert store.docs[ids[0]]["tags"] == ["mindset", "consolidated", "anchor"]
    assert store.docs[ids[1]]["tags"] == ["consolidated", "anchor"]
    assert store.docs[ids[2]]["tags"] == ["consolidated"]
    assert own["tags"] == ["mindset", "consolidated", "anchor"]        # observed: the input list gains them too


@pytest.mark.parametrize("given,stored", [("state", "state"), ("STATE", "state"), ("principle", "principle"),
                                          ("system", "system"), ("alert", "alert"), ("event", "event"),
                                          ("gadget", "event")])
async def test_type_is_case_insensitive_and_an_unknown_type_is_an_event(store, given, stored):
    _, _, ids = await add(store, [{"text": "a fact", "type": given}])
    assert store.docs[ids[0]]["type"] == stored


async def test_defaults_are_type_event_state_current_priority_medium_and_reported_date_now(store):
    started = datetime.now(timezone.utc)
    _, _, ids = await add(store, [{"text": "a fact"}])
    doc = store.docs[ids[0]]
    assert (doc["type"], doc["state"], doc["context_priority"]) == ("event", "current", "medium")
    assert (doc["domain"], doc["temporal_class"], doc["ttl_days"], doc["context"]) == (None, None, None, None)
    assert started <= doc["reported_date"] <= datetime.now(timezone.utc)


async def test_given_attributes_are_stored(store):
    _, _, ids = await add(store, [
        # Reviewer edit (facts S3, T2): ARCHIVED replaces STALE (S3-D3); the sent ttl_days is not stored (D1, S3-D4).
        {"text": "a fact", "domain": "health", "temporal_class": "dynamic", "state": "archived", "context_priority": "high",
         "ttl_days": 30, "context": "said at lunch", "reported_date": "2026-03-04T10:00:00+00:00"},
        {"text": "another", "reported_date": datetime(2025, 1, 2, tzinfo=timezone.utc)},
    ])
    doc = store.docs[ids[0]]
    assert (doc["domain"], doc["temporal_class"], doc["state"], doc["context_priority"]) == (
        "health", "dynamic", "archived", "high")
    assert (doc["ttl_days"], doc["context"], doc["context_priority_rank"]) == (None, "said at lunch", 2)
    assert doc["reported_date"] == datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)
    assert store.docs[ids[1]]["reported_date"] == datetime(2025, 1, 2, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------ dedup, order, returns

async def test_skip_deduplication_stores_every_fact_and_a_rerun_stores_new_ones(store):
    batch = [{"text": "likes tea"}, {"text": "likes tea"}]
    assert (await add(store, batch, skip_deduplication=True))[:2] == (2, 0)
    assert (await add(store, batch, skip_deduplication=True))[:2] == (2, 0)
    assert len(store.docs) == 4


async def test_without_skip_a_fact_identical_to_a_current_one_is_skipped(store):
    await add(store, [{"text": "likes tea"}], skip_deduplication=True)
    saved, skipped, _ = await add(store, [{"text": "likes tea"}, {"text": "owns a car"}], skip_deduplication=False)
    assert (saved, skipped) == (1, 1) and sorted(d["text"] for d in store.docs.values()) == [
        "likes tea", "owns a car"]


async def test_dedup_compares_each_fact_with_the_earlier_ones_of_the_same_batch(store):
    saved, skipped, _ = await add(store, [{"text": "likes tea"}, {"text": "owns a car"}, {"text": "likes tea"}],
                                   skip_deduplication=False)
    assert (saved, skipped) == (2, 1) and len(store.docs) == 2


@pytest.mark.parametrize("setup", [
    {"account": OTHER}, {"state": FactState.ARCHIVED}, {"state": FactState.INVALIDATED}, {"state": FactState.SUPERSEDED}],
    ids=["other-account", "archived", "invalidated", "superseded"])
async def test_dedup_compares_only_current_facts_of_the_account(store, setup):
    await store.repo.add_fact(make_fact("likes tea", vector=embed("likes tea"), **setup))
    saved, skipped, _ = await add(store, [{"text": "likes tea"}], skip_deduplication=False)
    assert (saved, skipped) == (1, 0)


async def test_dedup_similarity_threshold_is_096(store):
    store.write._embedding = ControlledEmbedder({"likes tea": [1.0, 0.0, 0.0], "likes tea!": _near(0.97),
                                                 "likes tea?": _near(0.95)})
    await add(store, [{"text": "likes tea"}], skip_deduplication=True)
    assert (await add(store, [{"text": "likes tea?"}], skip_deduplication=False))[:2] == (1, 0)
    assert (await add(store, [{"text": "likes tea!"}], skip_deduplication=False))[:2] == (0, 1)


async def test_returns_saved_count_skipped_count_and_the_ids_in_input_order_when_dedup_is_skipped(store):
    saved, skipped, ids = await add(store, [{"text": "a fact"}, {"text": "b fact"}, {"text": "c fact"}],
                                    skip_deduplication=True)
    assert (saved, skipped) == (3, 0) and len(ids) == saved and texts_of(store, ids) == ["a fact", "b fact", "c fact"]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "known defect: add_facts_batch appends to saved_ids only when deduplication is skipped, so with dedup len(saved_ids) != "
    "saved_count — src/services/fact_write_service.py:188-199 (P4 T2 arbiter defect 4)"))
async def test_saved_ids_hold_every_stored_fact_in_input_order_with_deduplication(store):
    saved, skipped, ids = await add(store, [{"text": "a fact"}, {"text": "a fact"}, {"text": "c fact"}],
                                    skip_deduplication=False)
    assert (saved, skipped) == (2, 1)
    assert len(ids) == saved and texts_of(store, ids) == ["a fact", "c fact"]


async def test_empty_batch_returns_zeros_and_calls_nothing(store):
    embedder = ControlledEmbedder()
    store.write._embedding = embedder
    assert await add(store, []) == (0, 0, [])
    assert embedder.calls == 0 and store.docs == {}


# --------------------------------------------------------------------------- tenancy, atomicity and errors

async def test_other_accounts_documents_are_not_touched(store):
    put_raw(store, "foreign", "likes tea", account_id=OTHER)
    before = copy.deepcopy(store.docs["foreign"])
    await add(store, [{"text": "likes tea"}, {"text": "owns a car"}])
    assert store.docs["foreign"] == before and len(store.docs) == 3
    assert {d["account_id"] for i, d in store.docs.items() if i != "foreign"} == {ACC}


async def test_an_embedding_failure_stores_nothing(store):
    store.write._embedding = ControlledEmbedder(fail_on="third")
    with pytest.raises(RuntimeError):
        await add(store, [{"text": "first fact"}, {"text": "second fact"}, {"text": "third fact"}])
    assert store.docs == {}


async def test_a_later_failure_keeps_the_facts_already_stored(store, monkeypatch):
    # Reviewer edit (facts S2, D3/D-13): a bad value anywhere is now refused before any write, so the write failure that
    # keeps the facts already stored is a backend failure on a later fact; the bad-value case stores nothing.
    with pytest.raises(ValueError):
        await add(store, [{"text": "first fact"}, {"text": "second fact", "domain": "astrology"}, {"text": "third fact"}])
    assert store.docs == {}

    real_add = store.repo.add_fact

    async def fail_on_second(fact):
        if fact.text == "second fact":
            raise RuntimeError("backend down")
        return await real_add(fact)

    monkeypatch.setattr(store.repo, "add_fact", fail_on_second)
    with pytest.raises(RuntimeError):
        await add(store, [{"text": "first fact"}, {"text": "second fact"}, {"text": "third fact"}])
    assert [d["text"] for d in store.docs.values()] == ["first fact"]


@pytest.mark.parametrize("bad", [{"domain": "astrology"}, {"temporal_class": "sometimes"}, {"state": "zombie"},
                                 {"context_priority": "urgent"}])
async def test_an_invalid_enum_value_raises(store, bad):
    with pytest.raises(ValueError):
        await add(store, [{"text": "a fact", **bad}])
    assert store.docs == {}


@pytest.mark.parametrize("skip", [True, False])
async def test_a_backend_error_propagates(store, skip, monkeypatch):
    if skip:
        def boom(_doc_id):
            raise RuntimeError("backend down")
        monkeypatch.setattr(store.repo.facts_col, "document", boom)
    else:
        store.db.collection(FACTS).fail = RuntimeError("backend down")
    with pytest.raises(RuntimeError):
        await add(store, [{"text": "a fact"}], skip_deduplication=skip)
