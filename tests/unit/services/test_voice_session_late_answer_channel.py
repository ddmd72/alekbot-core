"""The late answer's channel is per provider (VOICE_COMPANION_RFC §4.7, amendment 2026-11-08).

A late answer is one that arrives after the caller spoke since the delegation was dispatched. OpenAI drops a late
function_call_output silently (spike 0.1), so it gets a system note. xAI ignores a late system note (it repeats its last
line) and speaks the call's function_call_output (live probe), so it gets that, followed by the usual reply.

Both providers run through their real adapters over a scripted websocket, so the wire is what is asserted.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.services.voice_session_service import _DELEGATION_TIMED_OUT, VoiceSessionService

_ANSWER = "Springfield is sunny at 21 degrees with a light westerly wind."
_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]
_CALL_ID = "call-1"


class _ScriptedWebSocket:
    """Yields scripted provider events in order; an async callable in the script runs at its place (the test's hook
    to release a delegation between events). When the script is done the provider goes quiet."""

    def __init__(self, script: list):
        self.sent: list = []
        self._script = script
        self.marks: dict = {}

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def mark(self, name: str):
        async def _mark():
            self.marks[name] = len(self.sent)
        return _mark

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._script:
            step = self._script.pop(0)
            if callable(step):
                await step()
                continue
            return json.dumps(step)
        await asyncio.sleep(10)
        raise StopAsyncIteration  # pragma: no cover

    async def close(self) -> None:
        pass


def _response(*between):
    return [{"type": "response.created", "response": {}}, *between,
            {"type": "response.done", "response": {"status": "completed", "output": []}, "usage": {}}]


def _tool_call():
    return {"type": "response.function_call_arguments.done", "call_id": _CALL_ID, "name": "delegate_to_specialist",
            "arguments": json.dumps({"intent": "ask_alek", "query": "weather in Springfield"})}


def _released(release: asyncio.Event):
    async def _step():
        release.set()
        await asyncio.sleep(0.05)  # the answer goes in here
    return _step


async def _call(adapter, ws, provider, delegate, **kwargs):
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS,
        "voice": VOICE_PROVIDER_PROFILES[provider].session.to_dict()}
    control.delegate.side_effect = delegate

    async def inbound():
        await asyncio.sleep(0.4)
        return
        yield  # pragma: no cover

    await VoiceSessionService(realtime_session_factory=MagicMock(return_value=adapter), control_plane=control,
                              alert_sink=AsyncMock(), **kwargs).handle_call(
        ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(), clear_outbound_audio=AsyncMock(),
        playback=PlaybackTracker())


def _gated(release: asyncio.Event, output=_ANSWER):
    async def delegate(**_):
        await release.wait()
        return output
    return delegate


def _tool_outputs(sent):
    return [s["item"] for s in sent if s["type"] == "conversation.item.create"
            and s["item"]["type"] == "function_call_output"]


def _system_texts(sent):
    return [s["item"]["content"][0]["text"] for s in sent if s["type"] == "conversation.item.create"
            and s["item"].get("role") == "system"]


def _creates(sent):
    return [s for s in sent if s["type"] == "response.create"]


class TestXaiLateAnswerIsTheCallsToolResult:
    async def test_after_the_caller_spoke_the_answer_is_the_calls_output_and_one_reply(self):
        release = asyncio.Event()
        script = [*_response(), *_response(_tool_call()),
                  {"type": "input_audio_buffer.speech_started", "item_id": "u2"},
                  {"type": "input_audio_buffer.speech_stopped", "item_id": "u2"},
                  *_response(),  # Grok replies to the caller's turn by itself
                  {"type": "input_audio_buffer.committed", "item_id": "u2"}]
        ws = _ScriptedWebSocket(script)
        script.append(ws.mark("released"))
        script.append(_released(release))
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))

        await _call(adapter, ws, "xai", _gated(release))

        after = ws.sent[ws.marks["released"]:]
        assert [s["type"] for s in after] == ["conversation.item.create", "response.create"]
        assert _tool_outputs(after) == [{"type": "function_call_output", "call_id": _CALL_ID, "output": _ANSWER}]
        assert not any(_ANSWER in text for text in _system_texts(ws.sent))
        assert len(_creates(ws.sent)) == 2  # the pickup and the answer's reply

    async def test_a_late_answer_while_the_caller_speaks_waits_for_their_turn_and_the_providers_reply_takes_it(self):
        release = asyncio.Event()
        script = [*_response(), *_response(_tool_call()),
                  {"type": "input_audio_buffer.speech_started", "item_id": "u2"}]
        ws = _ScriptedWebSocket(script)
        script += [_released(release), ws.mark("answered"),
                   {"type": "input_audio_buffer.speech_stopped", "item_id": "u2"},
                   *_response(),  # Grok's own reply to the turn carries the answer in context
                   {"type": "input_audio_buffer.committed", "item_id": "u2"}]
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))

        await _call(adapter, ws, "xai", _gated(release))

        assert _tool_outputs(ws.sent) == [{"type": "function_call_output", "call_id": _CALL_ID, "output": _ANSWER}]
        assert len(_creates(ws.sent)) == 1  # the pickup only: never a create over the caller or a second reply
        assert not any(_ANSWER in text for text in _system_texts(ws.sent))

    async def test_a_late_timeout_is_the_calls_output_too(self):
        script = [*_response(), *_response(_tool_call()),
                  {"type": "input_audio_buffer.speech_started", "item_id": "u2"},
                  {"type": "input_audio_buffer.speech_stopped", "item_id": "u2"},
                  *_response(),
                  {"type": "input_audio_buffer.committed", "item_id": "u2"}]
        ws = _ScriptedWebSocket(script)
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws))

        async def never(**_):
            await asyncio.sleep(10)

        await _call(adapter, ws, "xai", never, delegation_timeout_s=0.1)

        assert _tool_outputs(ws.sent) == [{"type": "function_call_output", "call_id": _CALL_ID,
                                           "output": _DELEGATION_TIMED_OUT}]
        assert len(_system_texts(ws.sent)) == 1  # the pickup note only
        assert len(_creates(ws.sent)) == 2


class TestOpenAiLateAnswerStaysASystemNote:
    async def test_after_the_caller_spoke_the_answer_is_a_system_note_and_never_the_calls_output(self):
        release = asyncio.Event()
        script = [*_response(), *_response(_tool_call()),
                  {"type": "input_audio_buffer.speech_started", "item_id": "u2"},
                  {"type": "input_audio_buffer.speech_stopped", "item_id": "u2"},
                  {"type": "input_audio_buffer.committed", "item_id": "u2"},  # the relay replies to the turn
                  *_response()]
        ws = _ScriptedWebSocket(script)
        script.append(ws.mark("released"))
        script.append(_released(release))
        adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

        await _call(adapter, ws, "openai", _gated(release))

        after = ws.sent[ws.marks["released"]:]
        assert _tool_outputs(ws.sent) == []
        notes = _system_texts(after)
        assert len(notes) == 1 and "just arrived" in notes[0] and _ANSWER in notes[0]
        assert after[-1]["type"] == "response.create"

    async def test_without_a_caller_turn_the_answer_is_the_calls_output(self):
        release = asyncio.Event()
        script = [*_response(), *_response(_tool_call())]
        ws = _ScriptedWebSocket(script)
        script.append(_released(release))
        adapter = OpenAIRealtimeAdapter(api_key="sk-test", ws_connect=AsyncMock(return_value=ws))

        await _call(adapter, ws, "openai", _gated(release))

        assert _tool_outputs(ws.sent) == [{"type": "function_call_output", "call_id": _CALL_ID, "output": _ANSWER}]
        assert not any(_ANSWER in text for text in _system_texts(ws.sent))


class TestTheCapability:
    def test_xai_declares_it_and_openai_does_not(self):
        assert XaiRealtimeAdapter.late_tool_result_after_caller_turn is True
        assert OpenAIRealtimeAdapter.late_tool_result_after_caller_turn is False
