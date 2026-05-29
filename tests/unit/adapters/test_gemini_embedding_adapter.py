"""
Unit tests for GeminiEmbeddingAdapter (gemini-embedding-2 contract).

Pattern: mock adapter.client.models.embed_content at the SDK boundary,
call real adapter method, assert on what was passed to the SDK and what came back.

SDK boundary: self.client.models.embed_content
(synchronous genai call wrapped via asyncio.to_thread internally).

v2 contract (see decisions/embedding_model_migration_v1_to_v2.md):
  - model:                 "gemini-embedding-2"
  - output_dimensionality: 768 (Matryoshka truncation)
  - task_type:             REMOVED from SDK config; translated to inline prefix:
      RETRIEVAL_DOCUMENT   → "title: | text: {content}"
      RETRIEVAL_QUERY      → "task: search result | query: {content}"
      SEMANTIC_SIMILARITY  → "{content}"  (passthrough)
      unknown              → ValueError
"""
import pytest
from unittest.mock import MagicMock, patch

from src.adapters.gemini_embedding_adapter import (
    GeminiEmbeddingAdapter,
    _apply_task_prefix,
)


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
    """Minimal result object from client.models.embed_content (single call).

    v2 note: the adapter's batch path fans out N parallel single-content calls;
    use `_set_distinct_responses` below to configure per-call returns.
    """
    if batch_values is None:
        batch_values = [[0.1, 0.2]]
    embeddings = []
    for vals in batch_values:
        e = MagicMock()
        e.values = vals
        embeddings.append(e)
    result = MagicMock()
    result.embeddings = embeddings
    return result


def _set_distinct_responses(adapter, values_per_call):
    """Return one distinct single-embedding result per call (in arrival order).

    Use this only when the test does NOT care which prefixed content maps to which
    vector — only that N distinct calls happened. `asyncio.gather` makes call
    arrival order non-deterministic; prefer `_set_content_keyed_responses` for
    order-sensitive assertions.
    """
    responses = [_make_embedding_result(vals) for vals in values_per_call]
    adapter.client.models.embed_content.side_effect = responses


def _set_content_keyed_responses(adapter, contents_to_values):
    """Bind each prefixed-contents string to a specific embedding vector.

    Robust to non-deterministic call arrival under asyncio.gather. The adapter's
    `get_embedding` does the prefixing, so the mapping keys must include the
    prefix (e.g. 'title: | text: …' for RETRIEVAL_DOCUMENT).
    """
    def side_effect(model=None, contents=None, config=None):
        if contents not in contents_to_values:
            raise AssertionError(f"Unexpected contents: {contents!r}")
        return _make_embedding_result(contents_to_values[contents])

    adapter.client.models.embed_content.side_effect = side_effect


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
async def test_get_embedding_prefixes_document_text():
    """Default task_type=RETRIEVAL_DOCUMENT → 'title: | text: <input>' prefix."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("test text")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["contents"] == "title: | text: test text"


@pytest.mark.asyncio
async def test_get_embedding_prefixes_query_text():
    """task_type=RETRIEVAL_QUERY → 'task: search result | query: <input>' prefix."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("find me X", task_type="RETRIEVAL_QUERY")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["contents"] == "task: search result | query: find me X"


@pytest.mark.asyncio
async def test_get_embedding_semantic_similarity_passthrough():
    """task_type=SEMANTIC_SIMILARITY → text passed through with no prefix."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("just compare this", task_type="SEMANTIC_SIMILARITY")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["contents"] == "just compare this"


@pytest.mark.asyncio
async def test_get_embedding_passes_v2_model():
    """Model is gemini-embedding-2, no 'models/' legacy prefix."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-embedding-2"


@pytest.mark.asyncio
async def test_get_embedding_config_omits_task_type():
    """v2 removed task_type from config — it must not appear there."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text", task_type="RETRIEVAL_DOCUMENT")

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert "task_type" not in config


@pytest.mark.asyncio
async def test_get_embedding_passes_dimensionality_768():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    await adapter.get_embedding("text")

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_get_embedding_unknown_task_type_raises():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result()

    with pytest.raises(ValueError, match="Unsupported task_type"):
        await adapter.get_embedding("text", task_type="CLUSTERING")

    adapter.client.models.embed_content.assert_not_called()


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
    _set_distinct_responses(adapter, [[0.1, 0.2], [0.3, 0.4]])

    result = await adapter.get_embeddings_batch(["text one", "text two"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.asyncio
async def test_get_embeddings_batch_prefixes_each_document():
    """Batch with RETRIEVAL_DOCUMENT issues one prefixed single-text call per input.

    v2 model treats List[str] as multimodal parts of one document — the adapter
    works around this by fanning out N parallel single-content calls.
    """
    adapter = _make_adapter()
    # _embedding_result returns one embedding per call; each of the 2 calls returns the same mock.
    adapter.client.models.embed_content.return_value = _make_embedding_result([0.7, 0.8])

    await adapter.get_embeddings_batch(["a", "b"])

    # Parallel calls → arrival order at SDK is non-deterministic; assert as a set.
    contents_per_call = sorted(
        call.kwargs["contents"]
        for call in adapter.client.models.embed_content.call_args_list
    )
    assert contents_per_call == sorted(["title: | text: a", "title: | text: b"])


@pytest.mark.asyncio
async def test_get_embeddings_batch_prefixes_each_query():
    """Batch with RETRIEVAL_QUERY issues one prefixed single-text call per query."""
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_embedding_result([0.1, 0.2])

    await adapter.get_embeddings_batch(["q1", "q2"], task_type="RETRIEVAL_QUERY")

    contents_per_call = sorted(
        call.kwargs["contents"]
        for call in adapter.client.models.embed_content.call_args_list
    )
    assert contents_per_call == sorted([
        "task: search result | query: q1",
        "task: search result | query: q2",
    ])


@pytest.mark.asyncio
async def test_get_embeddings_batch_passes_v2_model():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    call_kwargs = adapter.client.models.embed_content.call_args.kwargs
    assert call_kwargs["model"] == "gemini-embedding-2"


@pytest.mark.asyncio
async def test_get_embeddings_batch_passes_dimensionality_768():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert config["output_dimensionality"] == 768


@pytest.mark.asyncio
async def test_get_embeddings_batch_config_omits_task_type():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    await adapter.get_embeddings_batch(["a", "b"])

    config = adapter.client.models.embed_content.call_args.kwargs["config"]
    assert "task_type" not in config


@pytest.mark.asyncio
async def test_get_embeddings_batch_unknown_task_type_raises():
    adapter = _make_adapter()
    adapter.client.models.embed_content.return_value = _make_batch_result()

    with pytest.raises(ValueError, match="Unsupported task_type"):
        await adapter.get_embeddings_batch(["a", "b"], task_type="CODE_RETRIEVAL_QUERY")

    adapter.client.models.embed_content.assert_not_called()


@pytest.mark.asyncio
async def test_get_embeddings_batch_preserves_order():
    """gather() preserves input → output order even when SDK calls land out of order."""
    adapter = _make_adapter()
    values = [[float(i)] * 3 for i in range(5)]
    _set_content_keyed_responses(
        adapter,
        {f"title: | text: text {i}": values[i] for i in range(5)},
    )

    result = await adapter.get_embeddings_batch([f"text {i}" for i in range(5)])

    assert len(result) == 5
    for i, row in enumerate(result):
        assert row == [float(i)] * 3


@pytest.mark.asyncio
async def test_get_embeddings_batch_calls_embed_content_per_text():
    """v2: gemini-embedding-2 has no true batch — adapter issues N parallel single-content calls."""
    adapter = _make_adapter()
    _set_distinct_responses(adapter, [[0.1] * 3, [0.2] * 3, [0.3] * 3])

    await adapter.get_embeddings_batch(["a", "b", "c"])

    assert adapter.client.models.embed_content.call_count == 3


# ============================================================================
# _apply_task_prefix — translator unit tests (pure function)
# ============================================================================

def test_apply_task_prefix_retrieval_document():
    assert _apply_task_prefix("hello", "RETRIEVAL_DOCUMENT") == "title: | text: hello"


def test_apply_task_prefix_retrieval_query():
    assert _apply_task_prefix("find X", "RETRIEVAL_QUERY") == "task: search result | query: find X"


def test_apply_task_prefix_semantic_similarity_passthrough():
    assert _apply_task_prefix("hello", "SEMANTIC_SIMILARITY") == "hello"


def test_apply_task_prefix_unknown_raises():
    with pytest.raises(ValueError, match="Unsupported task_type"):
        _apply_task_prefix("hello", "QUESTION_ANSWERING")


def test_apply_task_prefix_unknown_lists_supported():
    """Error message must list the supported task types so callers can self-correct."""
    with pytest.raises(ValueError) as exc_info:
        _apply_task_prefix("hello", "CLASSIFICATION")
    msg = str(exc_info.value)
    assert "RETRIEVAL_DOCUMENT" in msg
    assert "RETRIEVAL_QUERY" in msg
    assert "SEMANTIC_SIMILARITY" in msg


def test_apply_task_prefix_empty_text():
    """Empty input is allowed — the prefix is still applied."""
    assert _apply_task_prefix("", "RETRIEVAL_DOCUMENT") == "title: | text: "
    assert _apply_task_prefix("", "RETRIEVAL_QUERY") == "task: search result | query: "
