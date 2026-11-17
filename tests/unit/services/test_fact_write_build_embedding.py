"""FactWriteService.build_facts embeds the shared recipe's texts in one document-task batch, and refuses a wrong vector count."""
import pytest

from src.domain.fact_embedding import fact_embedding_texts
from src.domain.new_fact_attributes import parse_new_fact
from src.ports.embedding_service import EmbeddingService
from src.services.fact_write_service import FactWriteService
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import embed


class TaskRecordingEmbedder(EmbeddingService):
    def __init__(self, count=None):
        self.batches = []
        self.count = count

    async def get_embedding(self, text, task_type="RETRIEVAL_DOCUMENT"):
        raise AssertionError("build_facts embeds through the batch call only")

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.batches.append((list(texts), task_type))
        vectors = [embed(t) for t in texts]
        return vectors if self.count is None else (vectors + [embed("extra")] * self.count)[:self.count]


async def test_each_fact_is_one_document_batch_of_the_recipe_texts_taken_before_the_system_tags():
    embedder = TaskRecordingEmbedder()
    service = FactWriteService(embedder)
    text, tags, metadata = "Mother is Марія", ["family"], {"name": "Марія"}

    (entity,) = await service.build_facts(ACC, USER, [parse_new_fact(
        {"text": text, "tags": tags, "metadata": metadata, "type": "principle"})])

    expected = fact_embedding_texts(text, ["family"], metadata)
    assert embedder.batches == [(expected, "RETRIEVAL_DOCUMENT")]
    assert expected[1] == "family" and entity.tags == ["family", "consolidated", "anchor"]
    assert (entity.vector, entity.tags_vector, entity.metadata_vector) == tuple(embed(t) for t in expected)


@pytest.mark.parametrize("count", [0, 2, 4])
async def test_a_vector_count_other_than_three_is_refused(count):
    embedder = TaskRecordingEmbedder(count=count)

    with pytest.raises(RuntimeError, match="no vector"):
        await FactWriteService(embedder).build_facts(ACC, USER, [parse_new_fact({"text": "a fact"})])
    assert len(embedder.batches) == 1
