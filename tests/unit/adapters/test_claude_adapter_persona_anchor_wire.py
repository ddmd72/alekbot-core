"""ClaudeAdapter puts the persona anchor next to USER_TURN_SYSTEM_ANCHOR.

Claude has no `developer` role, so the turn anchor stays in the user message (it is followed well there).
OpenAI and Grok add the persona block to their developer item; Claude used to get none. The block now rides
right after the turn anchor, in the same channel. Wire test: asserted on what reaches `client.messages.stream`.
"""
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.llm import (
    USER_TURN_SYSTEM_ANCHOR,
    LLMRequest,
    Message,
    MessagePart,
    ToolCall,
    build_persona_anchor,
)
from tests.unit.adapters.test_claude_adapter import _make_claude_cm, _make_sdk_response

PERSONA_PROMPT = "identity {\n a\n}\nvoice {\n b\n}\nhumor_engine {\n c\n}"
PLAIN_PROMPT = "knowledge_base {\n x\n}"
PERSONA_ANCHOR = build_persona_anchor(PERSONA_PROMPT)


def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


async def _sent_messages(system: str, messages):
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return _make_claude_cm(_make_sdk_response("OK"))

    adapter.client.messages.stream = capturing_stream
    await adapter.generate_content(
        request=LLMRequest(model_name="claude-sonnet-5-5", system_instruction=system, messages=messages)
    )
    return captured


def _all_text(sent_messages) -> str:
    chunks = []
    for m in sent_messages:
        content = m["content"]
        if isinstance(content, str):
            chunks.append(content)
            continue
        chunks.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "\n".join(chunks)


@pytest.mark.asyncio
async def test_persona_anchor_follows_the_turn_anchor_in_the_user_message():
    captured = await _sent_messages(PERSONA_PROMPT, [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")])

    text = _all_text(captured["messages"])
    assert f"{USER_TURN_SYSTEM_ANCHOR}\n\n{PERSONA_ANCHOR}\n\nhello" in text


@pytest.mark.asyncio
async def test_persona_anchor_never_reaches_the_system_prompt():
    captured = await _sent_messages(PERSONA_PROMPT, [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")])

    assert "PERSONALITY ANCHOR" not in str(captured["system"])


@pytest.mark.asyncio
async def test_no_persona_sections_means_no_persona_anchor():
    """Specialists and bound channels have no persona to reinforce."""
    captured = await _sent_messages(PLAIN_PROMPT, [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")])

    assert "PERSONALITY ANCHOR" not in _all_text(captured["messages"])


@pytest.mark.asyncio
async def test_no_turn_anchor_means_no_persona_anchor():
    """Without the turn anchor (specialist call) there is nothing to attach to."""
    captured = await _sent_messages(PERSONA_PROMPT, [_user("hello")])

    assert "PERSONALITY ANCHOR" not in _all_text(captured["messages"])


def test_anchor_deeper_in_a_tool_loop_is_found_once():
    """From delegation turn 2 the last user message is a tool result; the anchor sits further up."""
    history = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello"),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(name="t", args={}))]),
        Message(role="user", parts=[MessagePart(tool_response={"name": "t", "response": "r"})]),
    ]

    out = ClaudeAdapter._add_persona_anchor(history, PERSONA_PROMPT)

    assert PERSONA_ANCHOR in out[0].parts[0].text
    assert out[0].parts[0].text.count("PERSONALITY ANCHOR") == 1
    assert out[1:] == history[1:]


def test_input_messages_are_not_mutated():
    original = _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")
    ClaudeAdapter._add_persona_anchor([original], PERSONA_PROMPT)

    assert "PERSONALITY ANCHOR" not in original.parts[0].text
