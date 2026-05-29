import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter

@pytest.mark.requirement("REQ-MEM-04")
@pytest.mark.asyncio
async def test_embedding_generation():
    """
    Test that GeminiEmbeddingAdapter generates embeddings for semantic retrieval.
    Covers: REQ-MEM-04 (Semantic Retrieval)
    """
    # Mock the genai client at adapter module level
    with patch("src.adapters.gemini_embedding_adapter.genai") as mock_genai:
        mock_client = MagicMock()
        mock_genai.Client.return_value = mock_client

        # Mock the synchronous embed_content call (adapter wraps it in asyncio.to_thread)
        mock_response = MagicMock()
        mock_response.embeddings = [MagicMock(values=[0.1, 0.2, 0.3])]
        mock_client.models.embed_content.return_value = mock_response

        service = GeminiEmbeddingAdapter(api_key="test-key")

        # Execute
        vector = await service.get_embedding("test text")

        # Verify
        assert vector == [0.1, 0.2, 0.3]
        mock_client.models.embed_content.assert_called_once()
        call_args = mock_client.models.embed_content.call_args
        assert call_args.kwargs['model'] == "gemini-embedding-2"
        assert call_args.kwargs['contents'] == "title: | text: test text"
