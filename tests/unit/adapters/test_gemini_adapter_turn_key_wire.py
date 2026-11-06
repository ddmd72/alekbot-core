"""GeminiAdapter keyed turn anchor (SYSTEM_TURN_FINGERPRINT_RFC §4.8), mocked at `client.aio.models.generate_content`.
No persona text on Gemini; the boundary strip is unchanged, so the session_key block ends the system instruction."""
from unittest.mock import MagicMock

from src.domain.llm import PROMPT_CACHE_BOUNDARY, USER_TURN_SYSTEM_ANCHOR
from src.domain.system_fingerprint import render_session_key, wrap_system
from src.ports.llm_port import LLMRequest, Message, MessagePart
from tests.contracts.adapter_contracts import TURN_ANCHOR_KEYED_WITH_TURN_KEY
from tests.unit.adapters.test_gemini_adapter_inline_bytes_wire import _adapter

KEY = "lamp-orbit-fern"


def _user(text: str) -> Message:
    return Message(role="user", parts=[MessagePart(text=text)])


async def _sent(messages, system="read", turn_key=None):
    adapter, captured = _adapter()
    fields = dict(model_name="gemini-3.8-flash", system_instruction=system, messages=messages)
    if turn_key is not None:
        fields["turn_key"] = turn_key
    await adapter.generate_content(request=LLMRequest(**fields))
    return captured


def _texts(captured):
    return [[p.text for p in c.parts] for c in captured["contents"]]


async def test_key_wraps_the_turn_anchor():
    captured = await _sent([_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")], turn_key=KEY)

    assert _texts(captured) == [[wrap_system(USER_TURN_SYSTEM_ANCHOR, KEY) + "\n\nhello"]]
    assert "PERSONALITY ANCHOR" not in str(_texts(captured))
    TURN_ANCHOR_KEYED_WITH_TURN_KEY.validate("gemini", {**captured, "expected_key": KEY, "user_text": "hello"})


async def test_no_key_leaves_contents_as_they_were():
    captured = await _sent([_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")])

    assert _texts(captured) == [[f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello"]]


async def test_session_key_block_ends_the_system_instruction_without_the_boundary():
    system = "STATIC\n\n" + PROMPT_CACHE_BOUNDARY + "\n" + render_session_key(KEY)
    captured = await _sent([_user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello")], system=system, turn_key=KEY)

    instruction = captured["config"].system_instruction
    assert instruction.endswith(render_session_key(KEY))
    assert PROMPT_CACHE_BOUNDARY not in instruction


async def test_anchor_in_an_earlier_user_message_is_keyed_on_turn_two():
    history = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nwhat does Tom's plan say about the review?"),
        Message(role="model", parts=[MessagePart(text="checking")]),
        _user("thanks"),
    ]
    captured = await _sent(history, turn_key=KEY)

    texts = _texts(captured)
    assert texts[0][0].startswith(wrap_system(USER_TURN_SYSTEM_ANCHOR, KEY))
    assert texts[2] == ["thanks"]


async def test_a_pasted_anchor_in_a_later_user_part_does_not_capture_the_key():
    pasted = f"look at this line: {USER_TURN_SYSTEM_ANCHOR} and answer"
    history = [
        _user(f"{USER_TURN_SYSTEM_ANCHOR}\n\nhello"),
        Message(role="model", parts=[MessagePart(text="ok")]),
        _user(pasted),
    ]
    captured = await _sent(history, turn_key=KEY)

    texts = _texts(captured)
    assert texts[0][0] == wrap_system(USER_TURN_SYSTEM_ANCHOR, KEY) + "\n\nhello"
    assert texts[2] == [pasted]
