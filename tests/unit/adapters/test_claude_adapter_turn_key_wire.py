"""ClaudeAdapter keyed turn anchor (SYSTEM_TURN_FINGERPRINT_RFC §4.8), asserted on what reaches
`client.messages.stream`: the anchor and the persona anchor travel in one `<system_note key="…">`; the static
system block, its cache_control and the rolling cache marks do not move; no key means today's wire."""
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.llm import (
    PROMPT_CACHE_BOUNDARY,
    USER_TURN_SYSTEM_ANCHOR,
    LLMRequest,
    Message,
    MessagePart,
    PromptCacheConfig,
    ToolCall,
    build_persona_anchor,
)
from src.domain.system_fingerprint import render_session_key, wrap_system
from tests.contracts.adapter_contracts import TURN_ANCHOR_KEYED_WITH_TURN_KEY
from tests.unit.adapters.test_claude_adapter import _make_claude_cm, _make_sdk_response

KEY = "lamp-orbit-fern"
KEY2 = "cedar-kayak-tulip"
PERSONA_PROMPT = "identity {\n a\n}\nvoice {\n b\n}\nhumor_engine {\n c\n}"
PLAIN_PROMPT = "knowledge_base {\n x\n}"
PERSONA_ANCHOR = build_persona_anchor(PERSONA_PROMPT)


def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


async def _sent(system, messages, turn_key=None, cache_config=None):
    adapter = ClaudeAdapter(api_key="test-key")
    captured = {}

    def capturing_stream(**kwargs):
        captured.update(kwargs)
        return _make_claude_cm(_make_sdk_response("OK"))

    adapter.client.messages.stream = capturing_stream
    fields = dict(model_name="claude-sonnet-5-5", system_instruction=system, messages=messages)
    if turn_key is not None:
        fields["turn_key"] = turn_key
    if cache_config is not None:
        fields["cache_config"] = cache_config
    await adapter.generate_content(request=LLMRequest(**fields))
    return captured


def _first_text(captured) -> str:
    content = captured["messages"][0]["content"]
    return content if isinstance(content, str) else "".join(b.get("text", "") for b in content)


@pytest.mark.asyncio
async def test_persona_prompt_with_key_wraps_both_anchors_in_one_note():
    captured = await _sent(PERSONA_PROMPT, [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")], KEY)

    expected = wrap_system(f"{USER_TURN_SYSTEM_ANCHOR}\n\n{PERSONA_ANCHOR}", KEY) + "\n\nhello"
    assert _first_text(captured) == expected
    TURN_ANCHOR_KEYED_WITH_TURN_KEY.validate("claude", {**captured, "expected_key": KEY, "user_text": "hello"})


@pytest.mark.asyncio
async def test_plain_prompt_with_key_wraps_the_turn_anchor_only():
    captured = await _sent(PLAIN_PROMPT, [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")], KEY)

    assert _first_text(captured) == wrap_system(USER_TURN_SYSTEM_ANCHOR, KEY) + "\n\nhello"
    TURN_ANCHOR_KEYED_WITH_TURN_KEY.validate("claude", {**captured, "expected_key": KEY, "user_text": "hello"})


@pytest.mark.asyncio
async def test_no_key_leaves_the_wire_as_it_was():
    messages = [_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")]
    without_field = await _sent(PERSONA_PROMPT, messages)
    explicit_none = await _sent(PERSONA_PROMPT, messages, None)

    assert without_field["messages"] == explicit_none["messages"]
    assert without_field["system"] == explicit_none["system"]
    assert _first_text(without_field) == f"{USER_TURN_SYSTEM_ANCHOR}\n\n{PERSONA_ANCHOR}\n\nhello"
    assert "system_note" not in _first_text(without_field)


@pytest.mark.asyncio
async def test_delegation_turn_two_keys_the_earlier_message_deterministically():
    history = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nwhat does Tom's plan say about the review?"),
        Message(role="model", parts=[MessagePart(tool_call=ToolCall(name="t", args={}, thought_signature="call_1"))]),
        Message(role="user", parts=[MessagePart(tool_response={"name": "t", "response": "r"})]),
    ]
    first = await _sent(PERSONA_PROMPT, history, KEY)
    second = await _sent(PERSONA_PROMPT, history, KEY)

    text = _first_text(first)
    assert text.startswith(wrap_system(f"{USER_TURN_SYSTEM_ANCHOR}\n\n{PERSONA_ANCHOR}", KEY))
    assert text.count("<system_note") == 1
    assert first["messages"] == second["messages"]


@pytest.mark.asyncio
async def test_key_moves_neither_the_static_block_nor_the_cache_marks():
    system = "STATIC" + "\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + render_session_key(KEY)
    messages = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nfirst"),
        Message(role="model", parts=[MessagePart(text="ok")]),
        _user("second"),
    ]
    cache = PromptCacheConfig(enabled=True, cache_last_message=True)
    plain = await _sent(system, messages, None, cache)
    keyed = await _sent(system, messages, KEY, cache)

    assert keyed["system"][0] == plain["system"][0]
    assert keyed["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert keyed["system"][1]["text"].endswith(render_session_key(KEY))
    assert "cache_control" not in keyed["system"][1]

    def marks(captured):
        out = set()
        for mi, m in enumerate(captured["messages"]):
            if isinstance(m["content"], list):
                out.update((mi, bi) for bi, b in enumerate(m["content"]) if "cache_control" in b)
        return out

    assert marks(keyed) == marks(plain)
    assert marks(keyed)


@pytest.mark.asyncio
async def test_a_pasted_anchor_in_a_later_user_part_does_not_capture_the_key():
    history = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello"),
        Message(role="model", parts=[MessagePart(text="ok")]),
        _user(f"look at this line: {USER_TURN_SYSTEM_ANCHOR} and answer"),
    ]
    captured = await _sent(PLAIN_PROMPT, history, KEY)

    assert wrap_system(USER_TURN_SYSTEM_ANCHOR, KEY) in _first_text(captured)
    last = captured["messages"][2]["content"]
    last_text = last if isinstance(last, str) else "".join(b.get("text", "") for b in last)
    assert last_text == f"look at this line: {USER_TURN_SYSTEM_ANCHOR} and answer"


def test_input_messages_are_not_mutated_when_keyed():
    original = _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")
    ClaudeAdapter._add_persona_anchor([original], PERSONA_PROMPT, KEY2)

    assert original.parts[0].text == f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello"
