"""OpenAI and Grok read no turn_key (SYSTEM_TURN_FINGERPRINT_RFC §4.8): they keep their exact-string developer-item
move, so the SDK kwargs are the same with and without the key."""
import pytest

from src.adapters.grok_adapter import GrokAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.llm import USER_TURN_SYSTEM_ANCHOR
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.unit.adapters.test_grok_adapter import _install, _make_response as _grok_response
from tests.unit.adapters.test_openai_adapter import _make_response as _openai_response

KEY = "lamp-orbit-fern"


def _request(model, turn_key):
    return LLMRequest(
        model_name=model,
        system_instruction="identity {\n a\n}\nvoice {\n b\n}\nhumor_engine {\n c\n}",
        messages=[Message(role="user", parts=[MessagePart(text=f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")])],
        turn_key=turn_key,
    )


async def _openai_kwargs(turn_key):
    adapter = OpenAIAdapter(api_key="test-key")
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return _openai_response(text="OK")

    adapter.client.responses.create = create
    await adapter.generate_content(request=_request("gpt-5-mini", turn_key))
    return captured


async def _grok_kwargs(turn_key):
    adapter = GrokAdapter(api_key="test-key")
    captured = {}
    _install(adapter, _grok_response("OK"), captured)
    await adapter.generate_content(request=_request("grok-4.6", turn_key))
    return captured


@pytest.mark.asyncio
async def test_openai_kwargs_are_the_same_with_and_without_a_key():
    assert await _openai_kwargs(None) == await _openai_kwargs(KEY)


@pytest.mark.asyncio
async def test_grok_kwargs_are_the_same_with_and_without_a_key():
    assert await _grok_kwargs(None) == await _grok_kwargs(KEY)
