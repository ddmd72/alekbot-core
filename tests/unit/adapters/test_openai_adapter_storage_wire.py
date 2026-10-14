"""Wire tests: `LLMRequest.allow_provider_storage` on the OpenAI Responses API (USER_DRIVE_INDEXING_RFC §24.2). The
drive agents forbid storage (`store=false`): verbatim scans of the user's files must not be kept at OpenAI. Every
other caller keeps today's `store=true`. Mock boundary: AsyncOpenAI.responses.create."""
from unittest.mock import AsyncMock, MagicMock

from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart


def _adapter():
    resp = MagicMock(status="completed", output=[], output_text="ok")
    resp.usage = MagicMock(input_tokens=1, output_tokens=1, total_tokens=2,
                           input_tokens_details=MagicMock(cached_tokens=0, cache_write_tokens=0))
    adapter = OpenAIAdapter(api_key="k")
    adapter.client.responses.create = AsyncMock(return_value=resp)
    return adapter


def _request(**kw):
    return LLMRequest(model_name="gpt-6-luna", messages=[Message(role="user", parts=[MessagePart(text="hi")])], **kw)


async def test_storage_forbidden_sends_store_false():
    adapter = _adapter()
    await adapter.generate_content(_request(allow_provider_storage=False))
    assert adapter.client.responses.create.call_args.kwargs["store"] is False


async def test_default_keeps_store_true():
    adapter = _adapter()
    await adapter.generate_content(_request())
    assert adapter.client.responses.create.call_args.kwargs["store"] is True


def test_default_allows_storage():
    assert _request().allow_provider_storage is True
