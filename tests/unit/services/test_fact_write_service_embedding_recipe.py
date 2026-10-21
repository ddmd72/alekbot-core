"""FactWriteService embeds exactly the shared recipe's texts."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.fact_embedding import fact_embedding_texts
from src.services.fact_write_service import FactWriteService


@pytest.mark.asyncio
async def test_batch_embeds_recipe_texts_before_consolidated_tag():
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(return_value=[[0.1] * 3] * 3)
    repo = MagicMock()
    repo.add_fact_if_unique = AsyncMock(return_value=(True, None))
    repo.add_fact = AsyncMock(return_value="id")
    service = FactWriteService(repo, embedding)

    text, tags, metadata = "Mother is Валентина", ["family"], {"name": "Валентина"}
    expected = fact_embedding_texts(text, list(tags), metadata)

    await service.add_facts_batch(
        "acc-1", "usr-1", [{"text": text, "tags": tags, "metadata": metadata, "type": "state"}]
    )

    embedding.get_embeddings_batch.assert_awaited_once_with(expected, "RETRIEVAL_DOCUMENT")
    assert expected[1] == "family"  # embedded before "consolidated" is appended
