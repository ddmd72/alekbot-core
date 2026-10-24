"""FactWritePort.build_facts and the parse-everything-first rule of add_facts_batch, on the evaluating fake store."""
import pytest

from src.domain.entities import FactState, FactType
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.new_fact_attributes import parse_new_fact
from src.ports.embedding_service import EmbeddingService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import FactStore, embed
from tests.support.firestore_eval import stored_vector


class CountingEmbedder(EmbeddingService):
    def __init__(self, none_for=None, count=None):
        self.calls = []
        self.none_for = none_for
        self.count = count

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        return embed(text)

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls.append(list(texts))
        vectors = [None if self.none_for == t else embed(t) for t in texts]
        return vectors if self.count is None else (vectors + [embed('extra')] * self.count)[:self.count]


@pytest.fixture
def store():
    return FactStore()


async def test_build_facts_embeds_and_writes_nothing(store):
    embedder = CountingEmbedder()
    store.write._embedding = embedder
    attrs = [parse_new_fact({"text": "mother is Maria", "tags": ["family"], "metadata": {"name": "Maria"}}),
             parse_new_fact({"text": "be calm", "type": "principle"})]

    entities = await store.write.build_facts(ACC, USER, attrs)

    assert store.docs == {}
    assert [e.text for e in entities] == ["mother is Maria", "be calm"]
    assert len({e.id for e in entities}) == 2 and len({e.lineage_id for e in entities}) == 2
    assert embedder.calls[0] == fact_embedding_texts("mother is Maria", ["family"], {"name": "Maria"})
    for entity, expected in zip(entities, embedder.calls):
        assert entity.vector == embed(expected[0])
        assert entity.tags_vector == embed(expected[1])
        assert entity.metadata_vector == embed(expected[2])
        assert (entity.account_id, entity.created_by_user_id, entity.is_current, entity.version) == (ACC, USER, True, 1)
    assert entities[0].tags == ["family", "consolidated"]
    assert entities[1].tags == ["consolidated", "anchor"]


async def test_build_facts_appends_system_tags_to_the_callers_own_list(store):
    own = ["mindset"]
    attrs = parse_new_fact({"text": "be calm", "tags": own, "type": "principle"})
    (entity,) = await store.write.build_facts(ACC, USER, [attrs])
    assert own == ["mindset", "consolidated", "anchor"] and entity.tags == own


async def test_build_facts_of_nothing_calls_nothing(store):
    embedder = CountingEmbedder()
    store.write._embedding = embedder
    assert await store.write.build_facts(ACC, USER, []) == []
    assert embedder.calls == []


async def test_build_facts_refuses_an_entity_without_a_vector(store):
    store.write._embedding = CountingEmbedder(none_for="no vector for me")
    with pytest.raises(RuntimeError):
        await store.write.build_facts(ACC, USER, [parse_new_fact({"text": "no vector for me"})])
    assert store.docs == {}


@pytest.mark.parametrize("count", [0, 2, 4])
async def test_build_facts_refuses_a_vector_count_other_than_three_and_writes_nothing(store, count):
    embedder = CountingEmbedder(count=count)
    store.write._embedding = embedder
    with pytest.raises(RuntimeError):
        await store.write.add_facts_batch(ACC, USER, [{"text": "a fact"}], skip_deduplication=True)
    assert len(embedder.calls) == 1 and store.docs == {}


@pytest.mark.parametrize("bad", [
    {"domain": "astrology"}, {"temporal_class": "sometimes"}, {"state": "zombie"}, {"context_priority": "urgent"},
    # Reviewer edit (facts S3, T2): `ttl_days` is no longer validated (D1); a malformed `expiry` is the new bad value.
    {"text": "  "}, {"tags": "family"}, {"expiry": {"kind": "soon"}}, {"reported_date": 5},
], ids=lambda b: next(iter(b)))
async def test_a_bad_value_anywhere_in_the_batch_fails_before_any_embedding_and_stores_nothing(store, bad):
    embedder = CountingEmbedder()
    store.write._embedding = embedder
    batch = [{"text": "first fact"}, {"text": "second fact", **bad}, {"text": "third fact"}]
    with pytest.raises(ValueError):
        await store.write.add_facts_batch(ACC, USER, batch, skip_deduplication=True)
    assert embedder.calls == [] and store.docs == {}


@pytest.mark.parametrize("skip", [True, False])
async def test_superseded_state_is_refused(store, skip):
    embedder = CountingEmbedder()
    store.write._embedding = embedder
    with pytest.raises(ValueError, match="SUPERSEDED"):
        await store.write.add_facts_batch(
            ACC, USER, [{"text": "a fact"}, {"text": "closed", "state": "superseded"}], skip_deduplication=skip)
    assert embedder.calls == [] and store.docs == {}


async def test_build_facts_entity_matches_add_facts_batch_fields(store):
    fact = {"text": "owns a Toyota", "tags": ["car"], "type": "STATE", "metadata": {"year": 2012}, "domain": "health",
            "temporal_class": "dynamic", "state": "archived", "context_priority": "high", "ttl_days": 30,
            "context": "said at lunch", "reported_date": "2026-03-04T10:00:00Z"}
    (built,) = await store.write.build_facts(ACC, USER, [parse_new_fact(dict(fact, tags=list(fact["tags"])))])
    _, _, (stored_id,) = await store.write.add_facts_batch(
        ACC, USER, [dict(fact, tags=list(fact["tags"]))], skip_deduplication=True)
    doc = store.docs[stored_id]

    for name in ("account_id", "created_by_user_id", "text", "tags", "metadata", "ttl_days", "context", "reported_date",
                 "is_current", "version", "replaces_fact_id", "superseded_by", "valid_to"):
        assert doc[name] == getattr(built, name), name
    for name in ("type", "domain", "temporal_class", "state", "context_priority"):
        assert doc[name] == getattr(built, name).value, name
    for name in ("vector", "tags_vector", "metadata_vector"):
        assert stored_vector(doc[name]) == pytest.approx(getattr(built, name))
    # Reviewer edit (facts S3, T2): ARCHIVED replaces STALE (S3-D3); the sent `ttl_days` is stored on neither path (D1).
    assert built.type is FactType.STATE and built.state is FactState.ARCHIVED
    assert built.ttl_days is None and doc["ttl_days"] is None
