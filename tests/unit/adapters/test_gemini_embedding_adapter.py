"""
Unit tests for GeminiEmbeddingAdapter.

Pattern: mock adapter.client.models.embed_content at the SDK boundary,
call real adapter method, assert on what was passed to the SDK and what came back.

SDK boundary: self.client.models.embed_content
(synchronous genai call wrapped via asyncio.to_thread internally).
"""
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter


# ============================================================================
# Helpers
# ============================================================================

def _make_embedding_result(values=None):
    """Minimal result object from client.models.embed_content (single text)."""
    if values is None:
        values = [0.1, 0.2, 0.3]
    embedding = MagicMock()
    embedding.values = values
    result = MagicMock()
    result.embeddings = [embedding]
    return result


def _make_batch_result(batch_values=None):
    """Minimal result object from client.models.embed_content (batch of texts)."""
    if batch_values is None:
        batch_values = [[0.1, 0.2], [0.3, 0.4]]
    embeddings = []
    for vals in batch_values:
        e = MagicMock()
        e.values = vals
        embeddings.append(e)
    result = MagicMock()
    result.embeddings = embeddings
    return result


def _make_adapter() -> GeminiEmbeddingAdapter:
    """Create adapter with mocked genai.Client (suppresses network I/O in __init__)."""
    with patch("src.adapters.gemini_embedding_adapter.genai.Client"):
        adapter = GeminiEmbeddingAdapter(api_key="test-key")
    adapter.client = MagicMock()
    return adapter


# ============================================================================
# Constructor validation
# ============================================================================

def test_empty_api_key_raises():
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiEmbeddingAdapter(api_key="")


# ============================================================================
# get_embedding — wire tests
# ============================================================================

@pytest.mark.asyncio
async def test_get_embedding_returns_values():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result([0.1, 0.2, 0.3])

    result = await adapter.get_embedding("hello world")

    assert result == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_get_embedding_passes_text_as_contents():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("test text")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["contents"] == "test text"


@pytest.mark.asyncio
async def test_get_embedding_passes_correct_model():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["model"] == "models/gemini-embedding-001"


@pytest.mark.asyncio
async def test_get_embedding_passes_default_task_type():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["task_type"] == "RETRIEVAL_DOCUMENT"


@pytest.mark.asyncio
async def test_get_embedding_passes_custom_task_type():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text", task_type="SEMANTIC_SIMILARITY")

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["task_type"] == "SEMANTIC_SIMILARITY"


@pytest.mark.asyncio
async def test_get_embedding_passes_dimensionality_768():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_get_embedding_calls_embed_content_once():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    adapter.client.models.embed_content.assert_called_once()


# ============================================================================
# get_embeddings_batch — wire tests
# ============================================================================

@pytest.mark.asyncio
async def test_get_embeddings_batch_returns_all_values():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result([[0.1, 0.2], [0.3, 0.4]])

    result = await adapter.get_embeddings_batch(["text one", "text two"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.asyncio
async def test_get_embeddings_batch_passes_texts_as_contents():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["contents"] == ["a", "b"]


@pytest.mark.asyncio
async def test_get_embeddings_batch_passes_correct_model():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["model"] == "models/gemini-embedding-001"


@pytest.mark.asyncio
async def test_get_embeddings_batch_passes_dimensionality_768():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_get_embeddings_batch_preserves_order():
    adapter = _make_adapter()
    values = [[float(i)] * 3 for i in range(5)]
    adapter.client.models.embed_content.return_value = _make_batch_result(values)

    result = await adapter.get_embeddings_batch([f"text {i}" for i in range(5)])

    assert len(result) == 5
    for i, row in enumerate(result):
        assert row == [float(i)] * 3


@pytest.mark.asyncio
async def test_get_embeddings_batch_calls_embed_content_once():
    """Batch uses a single API call, not N individual calls."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result(
        [[0.1] * 3, [0.2] * 3, [0.3] * 3]
    )

    await adapter.get_embeddings_batch(["a", "b", "c"])

    adapter.client.models.embed_content.assert_called_once()
