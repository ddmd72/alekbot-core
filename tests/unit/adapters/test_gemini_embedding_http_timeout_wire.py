"""
Wire tests: GeminiEmbeddingAdapter bounds every embedding request (USER_DRIVE_INDEXING_RFC §8.2).

google-genai sends with no HTTP timeout unless one is configured, so a hung connection held the
calling worker thread (and a drive tick) indefinitely. The client is built with a 30 s bound, and a
timeout reaches callers as LLMUnavailableError, the class a 503 already maps to: transient, retried
here, and a systemic stop for the drive tick.

Mock boundary: httpx.Client.send, under the real genai client (docs/how_to/ADAPTER_WIRE_TESTING.md),
so the SDK's own millisecond-to-second conversion is exercised too.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.domain.exceptions import LLMUnavailableError


def _hanging_send(sent):
    def send(self, request, **kwargs):
        sent.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)
    return send


async def test_every_request_carries_a_30s_timeout():
    sent = []
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx.Client, "send", _hanging_send(sent)), \
            patch("src.utils.retry.asyncio.sleep", new=AsyncMock()), pytest.raises(LLMUnavailableError):
        await adapter.get_embedding("hello")
    assert sent[0].extensions["timeout"] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


@pytest.mark.parametrize("timeout_cls", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_a_timeout_is_unavailable_and_retried_like_a_503(timeout_cls):
    sent = []

    def send(self, request, **kwargs):
        sent.append(request)
        raise timeout_cls("timed out", request=request)

    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx.Client, "send", send), \
            patch("src.utils.retry.asyncio.sleep", new=AsyncMock()), pytest.raises(LLMUnavailableError) as exc:
        await adapter.get_embedding("hello")
    assert isinstance(exc.value.__cause__, timeout_cls)
    assert exc.value.http_status is None          # no HTTP round trip completed
    assert len(sent) == 4                          # initial try + DEFAULT_RETRY_POLICY's 3 retries


async def test_batch_surfaces_the_timeout_as_unavailable():
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx.Client, "send", _hanging_send([])), \
            patch("src.utils.retry.asyncio.sleep", new=AsyncMock()), pytest.raises(LLMUnavailableError):
        await adapter.get_embeddings_batch(["a", "b"])
