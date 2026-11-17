"""FactWritePort.build_facts on the evaluating fake store."""
import pytest

from src.domain.fact_embedding import fact_embedding_texts
from src.domain.new_fact_attributes import parse_new_fact
from src.ports.embedding_service import EmbeddingService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import FactStore, embed


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


