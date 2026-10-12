"""Review fix R2b round 2 (premise_audit P9): a non-429 4xx from the embedding SDK reaches callers as a typed
LLMClientError carrying the HTTP status, so the drive indexer can tell a per-input refusal (400) from a config
failure (401/403/404) without importing the SDK.

Wire tests: mock adapter.client.models.embed_content at the SDK boundary (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import errors as genai_errors

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.domain.exceptions import LLMClientError


def _make_adapter() -> GeminiEmbeddingAdapter:
    with patch("src.adapters.gemini_embedding_adapter.genai.Client"):
        adapter = GeminiEmbeddingAdapter(api_key="test-key")
    adapter.client = MagicMock()
    return adapter


@pytest.mark.parametrize("code,status", [(400, "INVALID_ARGUMENT"), (403, "PERMISSION_DENIED")])
async def test_non_429_client_error_maps_to_llm_client_error_with_status(code, status):
    adapter = _make_adapter()
    err = genai_errors.ClientError(code=code, response_json={"error": {"message": "nope", "status": status}})
    adapter.client.models.embed_content.side_effect = err

    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(LLMClientError) as exc_info:
            await adapter.get_embedding("hi")

    assert exc_info.value.http_status == code
    assert exc_info.value.__cause__ is err
    assert adapter.client.models.embed_content.call_count == 1   # not in the transient retry set
    sleep.assert_not_awaited()


async def test_batch_propagates_the_typed_client_error():
    adapter = _make_adapter()
    err = genai_errors.ClientError(code=400, response_json={"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
    adapter.client.models.embed_content.side_effect = err

    with patch("src.utils.retry.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(LLMClientError) as exc_info:
            await adapter.get_embeddings_batch(["a", "b"])

    assert exc_info.value.http_status == 400
