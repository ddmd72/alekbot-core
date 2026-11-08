"""Wire tests (docs/how_to/ADAPTER_WIRE_TESTING.md, WebSocket boundary): both realtime adapters carry the provider's
`item_id` on `speech_started` and `speech_stopped` (VOICE_CALL_CONTROL_CHANNEL_RFC §5.4). The "Ask Lelik" latch
remembers the caller's turn its note marks by that id. The raw frames are the providers' own shapes; the socket is a
fake that replays them, so the adapter's real `receive_events` translation runs."""
import json
from unittest.mock import AsyncMock

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter


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


# xAI frames as the 2026-11-08 probe saw them (grok-voice-3.0): the item id is the turn's, before it is committed.
_XAI_FRAMES = [
    {"type": "input_audio_buffer.speech_started", "item_id": "item-u7", "audio_start_ms": 2140,
     "previous_item_id": "item-a6"},
    {"type": "input_audio_buffer.speech_stopped", "item_id": "item-u7", "audio_end_ms": 4980},
]
# OpenAI GA frames carry the same keys.
_OPENAI_FRAMES = [
    {"type": "input_audio_buffer.speech_started", "event_id": "event_1", "item_id": "item-u3",
     "audio_start_ms": 1000},
    {"type": "input_audio_buffer.speech_stopped", "event_id": "event_2", "item_id": "item-u3", "audio_end_ms": 2600},
]


async def _events(adapter_cls, frames: list[dict]) -> list:
    adapter = adapter_cls(api_key="key-0001-invented", ws_connect=AsyncMock(return_value=_ReplaySocket(frames)))
    await adapter.open(instructions="hi", reasoning_effort="high", tools=[])
    return [(event.type, event.payload) async for event in adapter.receive_events()]


@pytest.mark.asyncio
async def test_xai_speech_events_carry_the_item_id():
    assert await _events(XaiRealtimeAdapter, _XAI_FRAMES) == [
        ("speech_started", {"item_id": "item-u7"}),
        ("speech_stopped", {"item_id": "item-u7"}),
    ]


@pytest.mark.asyncio
async def test_openai_speech_events_carry_the_item_id():
    assert await _events(OpenAIRealtimeAdapter, _OPENAI_FRAMES) == [
        ("speech_started", {"item_id": "item-u3"}),
        ("speech_stopped", {"item_id": "item-u3"}),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_cls", [XaiRealtimeAdapter, OpenAIRealtimeAdapter])
async def test_a_speech_event_without_an_item_id_still_arrives_with_none(adapter_cls):
    frames = [{"type": "input_audio_buffer.speech_started"}, {"type": "input_audio_buffer.speech_stopped"}]
    assert await _events(adapter_cls, frames) == [
        ("speech_started", {"item_id": None}),
        ("speech_stopped", {"item_id": None}),
    ]
