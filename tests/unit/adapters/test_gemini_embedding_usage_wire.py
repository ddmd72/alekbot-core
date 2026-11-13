"""
Wire tests: GeminiEmbeddingAdapter meters the prompt tokens of every successful embedding call
(decisions/billing_embeddings.md).

google-genai >= 3.3 maps the Developer API's `usageMetadata` onto `EmbedContentResponse.usage_metadata`. The real SDK client
runs end to end here; the mock boundary is `httpx2.Client.send` (the SDK 3.x transport), returning the recorded
batchEmbedContents body shape: {"embeddings":[{"values":[...]}], "usageMetadata":{"promptTokenCount": N}}.
"""
import logging
from unittest.mock import AsyncMock, patch

import httpx2
import pytest

from src.adapters import gemini_embedding_adapter as mod
from src.adapters.gemini_embedding_adapter import GeminiEmbeddingAdapter
from src.domain.billing import ledger_scope


def _ok(tokens, values=(0.1, 0.2)):
    body = {"embeddings": [{"values": list(values)}]}
    if tokens is not None:
        body["usageMetadata"] = {"promptTokenCount": tokens, "totalTokenCount": tokens}
    return httpx2.Response(200, json=body)


def _send_with(responder, sent):
    def send(self, request, **kwargs):
        sent.append(request)
        return responder(request)
    return send


@pytest.fixture(autouse=True)
def fresh_meters(monkeypatch):
    monkeypatch.setattr(mod, "UNBILLED_EMBEDDING_TOKENS", mod.UnbilledEmbeddingTokens())
    monkeypatch.setattr(mod, "_NO_USAGE_LOG", mod._RateLimitedLog())


async def test_a_successful_call_adds_its_prompt_tokens_to_the_ledger():
    sent = []
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx2.Client, "send", _send_with(lambda r: _ok(16), sent)), ledger_scope("account-a") as ledger:
        vector = await adapter.get_embedding("text one")
    assert vector == [0.1, 0.2]
    assert sent[0].url.path.endswith(":batchEmbedContents") and "/v1beta/" in sent[0].url.path
    assert ledger.by_model["gemini-embedding-2"].prompt_tokens == 16
    assert ledger.by_model["gemini-embedding-2"].completion_tokens == 0
    assert mod.UNBILLED_EMBEDDING_TOKENS.tokens == 0


async def test_a_429_then_success_bills_only_the_success():
    sent = []
    answers = iter([
        httpx2.Response(429, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "slow down"}}),
        _ok(7),
    ])
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx2.Client, "send", _send_with(lambda r: next(answers), sent)), \
            patch("src.utils.retry.asyncio.sleep", new=AsyncMock()), ledger_scope("account-a") as ledger:
        await adapter.get_embedding("text one")
    assert len(sent) == 2
    assert ledger.by_model["gemini-embedding-2"].prompt_tokens == 7


async def test_a_response_without_usage_bills_nothing_and_warns_once(caplog):
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx2.Client, "send", _send_with(lambda r: _ok(None), [])), ledger_scope("account-a") as ledger, \
            caplog.at_level(logging.WARNING):
        await adapter.get_embedding("text one")
        await adapter.get_embedding("text two")
    assert ledger.is_empty and mod.UNBILLED_EMBEDDING_TOKENS.tokens == 0
    warnings = [r for r in caplog.records if "carried no usage_metadata; tokens unbilled" in r.getMessage()]
    assert len(warnings) == 1


async def test_without_a_scope_the_tokens_land_on_the_unbilled_tally(caplog):
    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx2.Client, "send", _send_with(lambda r: _ok(5), [])), caplog.at_level(logging.INFO):
        await adapter.get_embedding("text one")
        await adapter.get_embedding("text two")
    assert (mod.UNBILLED_EMBEDDING_TOKENS.tokens, mod.UNBILLED_EMBEDDING_TOKENS.calls) == (10, 2)
    lines = [r.getMessage() for r in caplog.records if "unbilled tokens=" in r.getMessage()]
    assert len(lines) == 1 and "(no billing scope)" in lines[0]   # at most once a minute


async def test_concurrent_calls_each_bill_their_own_tokens():
    def respond(request):
        text = request.content.decode()
        return _ok(11 if "alpha" in text else 29)

    adapter = GeminiEmbeddingAdapter(api_key="test-key")
    with patch.object(httpx2.Client, "send", _send_with(respond, [])), ledger_scope("account-a") as ledger:
        await adapter.get_embeddings_batch(["alpha", "beta", "beta"])
    assert ledger.by_model["gemini-embedding-2"].prompt_tokens == 11 + 29 + 29


async def test_tasks_of_different_scopes_do_not_mix():
    import asyncio

    adapter = GeminiEmbeddingAdapter(api_key="test-key")

    async def unit(text):
        with ledger_scope("account-a") as ledger:
            await adapter.get_embedding(text)
        return ledger.by_model["gemini-embedding-2"].prompt_tokens

    def respond(request):
        return _ok(3 if "alpha" in request.content.decode() else 8)

    with patch.object(httpx2.Client, "send", _send_with(respond, [])):
        assert await asyncio.gather(unit("alpha"), unit("beta")) == [3, 8]
