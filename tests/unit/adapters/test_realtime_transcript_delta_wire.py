"""Wire tests (docs/how_to/ADAPTER_WIRE_TESTING.md, WebSocket boundary): both realtime adapters map the streaming
transcript of Lelik's reply to `model_transcript_delta {item_id, text, audio_offset_ms}` and carry the caller's turn id
on `user_transcript` (VOICE_CALL_LIVE_TEXT_RFC §4.2). The socket is a fake that replays the providers' frames, so the
adapter's real `receive_events` translation runs.

The xAI delta frame is built from RFC §2 (the 2026-11-08 probe: each delta carries `start_time` in seconds into the
reply's audio and a `rid` shared with its audio chunks) plus the GA keys (`response_id`, `item_id`, `delta`): the raw
probe JSON is not kept in the repository. The first UAT call's "with offsets" count in the relay's teardown log checks
that `start_time` is read."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter

_LELIK = "Abbiamo una prenotazione con il codice RM-55120."
_CALLER = "We have a booking under RM-55120."
_MISSING = object()  # a key left out of the frame


class _ReplaySocket:
    def __init__(self, incoming: list[dict]):
        self._incoming = [json.dumps(event) for event in incoming]

    async def send(self, raw: str) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def close(self) -> None:
        pass


def _xai_delta(**overrides) -> dict:
    frame = {"type": "response.output_audio_transcript.delta", "response_id": "resp-a8", "item_id": "item-a8",
             "delta": _LELIK, "start_time": 2.24, "rid": "rid-a8"}
    frame.update(overrides)
    return {key: value for key, value in frame.items() if value is not _MISSING}



async def _events(adapter_cls, frames: list[dict]) -> list:
    adapter = adapter_cls(api_key="key-0001-invented", ws_connect=AsyncMock(return_value=_ReplaySocket(frames)))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[])
    return [(event.type, event.payload) async for event in adapter.receive_events()]


@pytest.mark.asyncio
async def test_an_xai_delta_carries_its_offset_in_ms():
    assert await _events(XaiRealtimeAdapter, [_xai_delta()]) == [
        ("model_transcript_delta", {"item_id": "item-a8", "text": _LELIK, "audio_offset_ms": 2240}),
    ]


@pytest.mark.asyncio
async def test_an_xai_delta_at_the_start_of_the_reply_has_offset_zero():
    events = await _events(XaiRealtimeAdapter, [_xai_delta(start_time=0)])
    assert events[0][1]["audio_offset_ms"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("start_time", [_MISSING, None, True, "2.24", float("nan")])
async def test_an_xai_delta_without_a_usable_start_time_has_no_offset(start_time):
    events = await _events(XaiRealtimeAdapter, [_xai_delta(start_time=start_time)])
    assert events == [("model_transcript_delta", {"item_id": "item-a8", "text": _LELIK, "audio_offset_ms": None})]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [XaiRealtimeAdapter, OpenAIRealtimeAdapter])
async def test_an_empty_delta_yields_no_event(adapter_cls):
    assert await _events(adapter_cls, [_xai_delta(delta=""), _xai_delta(delta=_MISSING)]) == []


@pytest.mark.asyncio
async def test_an_openai_delta_has_no_offset():
    frames = [{"type": "response.output_audio_transcript.delta", "event_id": "event_4", "response_id": "resp-a8",
               "item_id": "item-a8", "output_index": 0, "content_index": 0, "delta": _LELIK}]
    assert await _events(OpenAIRealtimeAdapter, frames) == [
        ("model_transcript_delta", {"item_id": "item-a8", "text": _LELIK, "audio_offset_ms": None}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [XaiRealtimeAdapter, OpenAIRealtimeAdapter])
async def test_the_caller_transcript_carries_its_item_id(adapter_cls):
    frames = [{"type": "conversation.item.input_audio_transcription.completed", "item_id": "item-u7",
               "content_index": 0, "transcript": _CALLER}]
    assert await _events(adapter_cls, frames) == [("user_transcript", {"text": _CALLER, "item_id": "item-u7"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [XaiRealtimeAdapter, OpenAIRealtimeAdapter])
async def test_a_caller_transcript_without_an_item_id_carries_none(adapter_cls):
    frames = [{"type": "conversation.item.input_audio_transcription.completed", "transcript": _CALLER}]
    assert await _events(adapter_cls, frames) == [("user_transcript", {"text": _CALLER, "item_id": None})]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [XaiRealtimeAdapter, OpenAIRealtimeAdapter])
async def test_the_final_transcript_is_still_the_model_transcript(adapter_cls):
    frames = [{"type": "response.output_audio_transcript.done", "item_id": "item-a8", "transcript": _LELIK}]
    assert await _events(adapter_cls, frames) == [("model_transcript", {"text": _LELIK})]
