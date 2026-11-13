"""Fable takes the caller's effort: adaptive thinking + output_config.effort on the wire.

Fable thinks always, so the adapter never sends a thinking off-switch for it; with an effort it
sends adaptive thinking and that effort. Asserted on what reaches `client.messages.stream`.
"""
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.llm import LLMRequest, Message, MessagePart
from tests.unit.adapters.test_claude_adapter import _make_claude_cm, _make_sdk_response

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_FABLE = "claude-fable-5-2"


async def _sent(**request_kwargs) -> dict:
    adapter = ClaudeAdapter(api_key="test-key")
    captured: dict = {}

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return _make_claude_cm(_make_sdk_response("OK"))

    adapter.client.messages.stream = capturing_stream
    await adapter.generate_content(
        request=LLMRequest(model_name=_FABLE, messages=_MESSAGES, **request_kwargs)
    )
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_effort_reaches_the_wire_as_adaptive_thinking(effort):
    sent = await _sent(thinking=effort)

    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["effort"] == effort


@pytest.mark.asyncio
async def test_effort_request_carries_no_budget_and_no_sampling():
    sent = await _sent(thinking="medium")

    assert "budget_tokens" not in sent["thinking"]
    for param in ("temperature", "top_p", "top_k"):
        assert param not in sent


@pytest.mark.asyncio
async def test_no_effort_never_sends_a_thinking_off_switch():
    """Thinking cannot be turned off on Fable: the field is omitted and the model runs adaptive."""
    sent = await _sent()

    assert "thinking" not in sent
    assert "effort" not in (sent.get("output_config") or {})
    for param in ("temperature", "top_p", "top_k"):
        assert param not in sent
