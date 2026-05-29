"""
Integration tests for GeminiEmbeddingAdapter — v2 protocol contract.

Wire-level verification at the SDK boundary using GeminiEmbeddingCapturingStub.
Covers the same invariants as the unit tests but against a stub closer to the real
SDK call surface — the unit tests use a MagicMock; here the stub uses an explicit
function signature matching `client.models.embed_content(model, contents, config)`.

No ContractRule was added in tests/contracts/adapter_contracts.py for this surface:
the embedding adapter is single-provider (Gemini only), so the rule repository's
cross-provider invariant rationale does not apply. The unit tests in
tests/unit/adapters/test_gemini_embedding_adapter.py are the authoritative spec;
this file verifies that the same invariants hold when wired through the real adapter
init path + SDK-call surface.
"""
import pytest

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from tests.integration.adapters.conftest import GeminiEmbeddingCapturingStub


@pytest.mark.asyncio
async def test_embed_single_document_wire_contract():
    """Single document call lands at SDK with v2 model + document prefix + 768 dim + no task_type."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub(batch_values=[[0.5] * 8]).install(adapter)

    result = await adapter.get_embedding("hello fact")

    assert result == [0.5] * 8
    assert stub.captured_kwargs["model"] == "gemini-embedding-2"
    assert stub.captured_kwargs["contents"] == "title: | text: hello fact"
    assert stub.captured_kwargs["config"] == {"output_dimensionality": 768}


@pytest.mark.asyncio
async def test_embed_single_query_wire_contract():
    """RETRIEVAL_QUERY path puts query prefix on the contents, model + dim unchanged."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub().install(adapter)

    await adapter.get_embedding("who is X", task_type="RETRIEVAL_QUERY")

    assert stub.captured_kwargs["contents"] == "task: search result | query: who is X"
    assert stub.captured_kwargs["model"] == "gemini-embedding-2"
    assert "task_type" not in stub.captured_kwargs["config"]


@pytest.mark.asyncio
async def test_embed_semantic_similarity_passthrough_wire():
    """SEMANTIC_SIMILARITY sends raw text — no prefix, no task_type."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub().install(adapter)

    await adapter.get_embedding("compare me", task_type="SEMANTIC_SIMILARITY")

    assert stub.captured_kwargs["contents"] == "compare me"
    assert "task_type" not in stub.captured_kwargs["config"]


@pytest.mark.asyncio
async def test_embed_batch_documents_wire_contract():
    """v2 batch fans out to N parallel single-content calls; each gets the doc prefix."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub(batch_values=[[1.0] * 8]).install(adapter)

    result = await adapter.get_embeddings_batch(["a", "b", "c"])

    # Stub returns the same vector for every parallel call → 3 identical results.
    assert result == [[1.0] * 8, [1.0] * 8, [1.0] * 8]
    assert len(stub.captured_calls) == 3
    # Parallel asyncio.gather → SDK call arrival order is non-deterministic; assert as a set.
    assert sorted(c["contents"] for c in stub.captured_calls) == sorted([
        "title: | text: a",
        "title: | text: b",
        "title: | text: c",
    ])
    for call in stub.captured_calls:
        assert call["model"] == "gemini-embedding-2"
        assert call["config"] == {"output_dimensionality": 768}


@pytest.mark.asyncio
async def test_embed_batch_queries_wire_contract():
    """v2 batch query path applies query prefix to every parallel call."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub(batch_values=[[0.1] * 8]).install(adapter)

    await adapter.get_embeddings_batch(["find X", "find Y"], task_type="RETRIEVAL_QUERY")

    assert len(stub.captured_calls) == 2
    # Parallel arrival → assert as a set, not by index.
    assert sorted(c["contents"] for c in stub.captured_calls) == sorted([
        "task: search result | query: find X",
        "task: search result | query: find Y",
    ])


@pytest.mark.asyncio
async def test_embed_rejects_unknown_task_type_before_sdk_call():
    """Unknown task_type must raise BEFORE any SDK call — the stub remains untouched."""
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub().install(adapter)

    with pytest.raises(ValueError, match="Unsupported task_type"):
        await adapter.get_embedding("text", task_type="CLASSIFICATION")

    assert stub.captured_kwargs == {}


@pytest.mark.asyncio
async def test_embed_batch_rejects_unknown_task_type_before_sdk_call():
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    stub = GeminiEmbeddingCapturingStub().install(adapter)

    with pytest.raises(ValueError, match="Unsupported task_type"):
        await adapter.get_embeddings_batch(["a", "b"], task_type="CODE_RETRIEVAL_QUERY")

    assert stub.captured_kwargs == {}
