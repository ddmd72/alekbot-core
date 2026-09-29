import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter, _flatten_usage
from src.domain.voice_audio_format import PCM16_24K
from src.domain.voice_audio_frame import AudioFrame
from tests.contracts.adapter_contracts import (
    XAI_REALTIME_REQUESTS_ARE_TAGGED,
    XAI_REALTIME_STRIPS_CACHE_BOUNDARY,
    XAI_REALTIME_TURN_DETECTION,
)

_OURS = {"origin": "relay"}
# Top-level usage as xAI sends it on response.done (probe 2026-09-29).
_USAGE = {
    "input_tokens": 394,
    "input_token_details": {"text_tokens": 18, "audio_tokens": 376, "grok_tokens": 0},
    "output_tokens": 1077,
    "output_token_details": {"text_tokens": 83, "audio_tokens": 994, "grok_tokens": 0},
    "total_tokens": 1471,
    "output_audio_seconds": 15.84,
    "billable_audio_seconds": 15,
}


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self.sent: list[dict] = []
        self._incoming = incoming

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def close(self) -> None:
        pass


async def _opened(incoming: list[dict], **kwargs) -> tuple[XaiRealtimeAdapter, FakeWebSocket]:
    ws = FakeWebSocket(incoming=incoming)
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=ws), **kwargs)
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[])
    ws.sent.clear()
    return adapter, ws


async def _events(adapter: XaiRealtimeAdapter) -> list:
    return [event async for event in adapter.receive_events()]


def _created(response_id: str, metadata=None) -> dict:
    response = {"id": response_id, "status": "in_progress"}
    if metadata is not None:
        response["metadata"] = metadata
    return {"type": "response.created", "response": response}


def _done(response_id: str, usage: dict) -> dict:
    return {"type": "response.done", "response_id": response_id,
            "response": {"id": response_id, "status": "completed"}, "usage": usage}


@pytest.mark.asyncio
async def test_open_sends_xai_session_shape():
    ws = FakeWebSocket(incoming=[])
    connect = AsyncMock(return_value=ws)
    adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=connect)
    tool = {"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}

    await adapter.open(
        instructions="You are Lelik.\n<!-- CACHE_BOUNDARY -->\nDynamic bit.",
        reasoning_effort="high",
        tools=[tool],
    )

    assert connect.await_args.args[0] == "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0"
    assert connect.await_args.kwargs["additional_headers"] == {"Authorization": "Bearer xai-test"}
    sent = ws.sent[0]
    assert sent["type"] == "session.update"
    session = sent["session"]
    assert session["voice"] == "rex"
    assert session["instructions"] == "You are Lelik.\n\nDynamic bit."
    assert session["audio"] == {"input": {"format": {"type": "audio/pcmu"}},
                                "output": {"format": {"type": "audio/pcmu"}}}
    assert session["reasoning"] == {"effort": "high"}
    assert session["tools"] == [{"type": "function", **tool}]
    assert session["tool_choice"] == "auto"
    assert "type" not in session  # OpenAI-GA-only field

    XAI_REALTIME_STRIPS_CACHE_BOUNDARY.validate("xai_realtime", sent)
    XAI_REALTIME_TURN_DETECTION.validate("xai_realtime", sent)


@pytest.mark.asyncio
async def test_open_pcm_format_names_its_rate_and_voice_is_overridable():
    ws = FakeWebSocket(incoming=[])
    adapter = XaiRealtimeAdapter(api_key="k", voice="leo", ws_connect=AsyncMock(return_value=ws), audio_format=PCM16_24K)
    await adapter.open(instructions="hi", reasoning_effort="none", tools=[])

    session = ws.sent[0]["session"]
    assert session["voice"] == "leo"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert "tools" not in session


@pytest.mark.asyncio
async def test_send_audio_forwards_base64_payload_unchanged():
    adapter, ws = await _opened([])
    await adapter.send_audio(AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=b"b64", track="inbound"))
    assert ws.sent == [{"type": "input_audio_buffer.append", "audio": "b64"}]


@pytest.mark.asyncio
async def test_request_response_is_tagged_with_origin_metadata():
    adapter, ws = await _opened([])
    await adapter.request_response()
    assert ws.sent == [{"type": "response.create", "response": {"metadata": _OURS}}]
    XAI_REALTIME_REQUESTS_ARE_TAGGED.validate("xai_realtime", ws.sent[0])


@pytest.mark.asyncio
async def test_requested_reply_passes_through_with_flat_usage_and_model_label():
    adapter, _ = await _opened([
        _created("r1", _OURS),
        {"type": "response.output_audio.delta", "response_id": "r1", "item_id": "i1", "delta": "QUJD"},
        {"type": "response.output_audio_transcript.done", "response_id": "r1", "item_id": "i1", "transcript": "Hi"},
        _done("r1", _USAGE),
    ])

    events = await _events(adapter)

    assert [e.type for e in events] == ["response_created", "audio_delta", "model_transcript", "response_done"]
    frame = events[1].payload["frame"]
    assert (frame.encoding, frame.sample_rate_hz, frame.payload, frame.track) == ("audio/pcmu", 8000, "QUJD", "outbound")
    assert events[1].payload["item_id"] == "i1"
    assert events[2].payload == {"text": "Hi"}
    assert events[3].payload == {"usage": _flatten_usage(_USAGE), "model": "grok-voice-think-fast-2.0"}


@pytest.mark.asyncio
async def test_unrequested_reply_is_cancelled_by_id_and_its_events_swallowed():
    adapter, ws = await _opened([
        _created("auto1"),
        {"type": "response.output_audio.delta", "response_id": "auto1", "item_id": "a", "delta": "x"},
        {"type": "response.output_audio_transcript.done", "response_id": "auto1", "item_id": "a", "transcript": "He"},
        {"type": "response.function_call_arguments.done", "response_id": "auto1", "call_id": "c", "name": "n", "arguments": "{}"},
        _done("auto1", _USAGE),
    ])

    events = await _events(adapter)

    assert events == []
    assert ws.sent == [{"type": "response.cancel", "response_id": "auto1"}]


@pytest.mark.asyncio
async def test_unrequested_reply_usage_is_carried_into_the_next_requested_reply():
    small = {"input_token_details": {"text_tokens": 2, "audio_tokens": 3},
             "output_token_details": {"text_tokens": 4, "audio_tokens": 5}, "billable_audio_seconds": 1}
    adapter, _ = await _opened([_created("auto1"), _done("auto1", small), _created("r1", _OURS), _done("r1", _USAGE)])

    events = await _events(adapter)

    assert [e.type for e in events] == ["response_created", "response_done"]
    assert events[1].payload["usage"] == {
        "audio_input_tokens": 376 + 3,
        "audio_output_tokens": 994 + 5,
        "text_input_tokens": 18 + 2,
        "text_output_tokens": 83 + 4,
        "billable_audio_seconds": 15 + 1,
    }


@pytest.mark.asyncio
async def test_carried_usage_is_billed_once():
    small = {"billable_audio_seconds": 1}
    adapter, _ = await _opened([
        _created("auto1"), _done("auto1", small),
        _created("r1", _OURS), _done("r1", {"billable_audio_seconds": 10}),
        _created("r2", _OURS), _done("r2", {"billable_audio_seconds": 20}),
    ])

    done = [e for e in await _events(adapter) if e.type == "response_done"]

    assert [e.payload["usage"]["billable_audio_seconds"] for e in done] == [11, 20]


@pytest.mark.asyncio
async def test_errors_from_cancelling_an_unrequested_reply_are_absorbed():
    adapter, _ = await _opened([
        _created("auto1"),
        {"type": "error", "error": {"message": "Response ID 'auto1' does not match current response"}},
        {"type": "error", "error": {"message": "Cancellation failed: no active response found"}},
    ])

    assert await _events(adapter) == []


@pytest.mark.asyncio
async def test_other_errors_still_reach_the_service():
    adapter, _ = await _opened([
        _created("auto1"),
        {"type": "error", "error": {"message": "Response ID 'r9' does not match current response"}},
        {"type": "error", "error": {"message": "Item not found: x"}},
    ])

    events = await _events(adapter)

    assert [e.type for e in events] == ["error", "error"]
    assert "r9" in events[0].payload["message"]


@pytest.mark.asyncio
async def test_no_active_response_error_reaches_the_service_when_nothing_was_self_cancelled():
    adapter, _ = await _opened([{"type": "error", "error": {"message": "Cancellation failed: no active response found"}}])

    events = await _events(adapter)

    assert [e.type for e in events] == ["error"]


@pytest.mark.asyncio
async def test_cancel_response_is_addressed_to_the_active_requested_reply():
    adapter, ws = await _opened([_created("r1", _OURS)])
    await _events(adapter)

    await adapter.cancel_response()

    assert ws.sent[-1] == {"type": "response.cancel", "response_id": "r1"}


@pytest.mark.asyncio
async def test_cancel_response_without_an_active_reply_is_unaddressed():
    adapter, ws = await _opened([_created("r1", _OURS), _done("r1", {})])
    await _events(adapter)

    await adapter.cancel_response()

    assert ws.sent[-1] == {"type": "response.cancel"}


@pytest.mark.asyncio
async def test_caller_side_events_are_normalized():
    adapter, _ = await _opened([
        {"type": "input_audio_buffer.speech_started", "item_id": "u1", "audio_start_ms": 2000},
        {"type": "input_audio_buffer.speech_stopped", "item_id": "u1", "audio_end_ms": 9000},
        {"type": "input_audio_buffer.committed", "item_id": "u1"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "u1", "transcript": "Привет"},
        {"type": "conversation.item.added", "item": {"id": "u1"}},
        {"type": "ping"},
    ])

    events = await _events(adapter)

    assert [(e.type, e.payload) for e in events] == [
        ("speech_started", {}),
        ("speech_stopped", {}),
        ("turn_committed", {"item_id": "u1"}),
        ("user_transcript", {"text": "Привет"}),
    ]


@pytest.mark.asyncio
async def test_tool_call_from_a_requested_reply_is_normalized():
    adapter, _ = await _opened([
        _created("r1", _OURS),
        {"type": "response.function_call_arguments.done", "response_id": "r1", "call_id": "c1",
         "name": "delegate_to_specialist", "arguments": "{\"query\": \"q\"}"},
    ])

    events = await _events(adapter)

    assert events[1].type == "tool_call"
    assert events[1].payload == {"call_id": "c1", "name": "delegate_to_specialist", "arguments": "{\"query\": \"q\"}"}


@pytest.mark.asyncio
async def test_submit_tool_result_message_and_truncate_shapes():
    adapter, ws = await _opened([])

    await adapter.submit_tool_result("c1", "Sunny")
    await adapter.submit_message("system", "anchor")
    await adapter.truncate("i1", 1500)

    assert ws.sent == [
        {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c1", "output": "Sunny"}},
        {"type": "conversation.item.create",
         "item": {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "anchor"}]}},
        {"type": "conversation.item.truncate", "item_id": "i1", "content_index": 0, "audio_end_ms": 1500},
    ]


def test_flatten_usage_maps_legs_and_tolerates_missing_details():
    assert _flatten_usage(_USAGE) == {
        "audio_input_tokens": 376,
        "audio_output_tokens": 994,
        "text_input_tokens": 18,
        "text_output_tokens": 83,
        "billable_audio_seconds": 15,
    }
    assert _flatten_usage({}) == {
        "audio_input_tokens": 0, "audio_output_tokens": 0, "text_input_tokens": 0,
        "text_output_tokens": 0, "billable_audio_seconds": 0,
    }
