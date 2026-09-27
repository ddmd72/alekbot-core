"""Wire test: the session's audio formats and outbound frames follow the injected AudioFormat."""
import json

import pytest

from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.domain.voice_audio_format import PCM16_24K


class FakeWs:
    def __init__(self, incoming=None):
        self.sent = []
        self._incoming = list(incoming or [])

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))


def _adapter(ws, **kwargs):
    async def connect(url, additional_headers):
        return ws
    return OpenAIRealtimeAdapter(api_key="k", ws_connect=connect, **kwargs)


@pytest.mark.asyncio
async def test_pcm24k_session_update_declares_pcm_at_24k_both_ways():
    ws = FakeWs()
    await _adapter(ws, audio_format=PCM16_24K).open(instructions="hi", reasoning_effort="low", tools=[])
    audio = ws.sent[0]["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio["output"]["format"] == {"type": "audio/pcm", "rate": 24000}


@pytest.mark.asyncio
async def test_default_stays_pcmu():
    ws = FakeWs()
    await _adapter(ws).open(instructions="hi", reasoning_effort="low", tools=[])
    assert ws.sent[0]["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}


@pytest.mark.asyncio
async def test_pcm24k_audio_delta_frame_is_labelled_pcm_24k():
    ws = FakeWs(incoming=[{"type": "response.output_audio.delta", "delta": "AAAA", "item_id": "i1"}])
    adapter = _adapter(ws, audio_format=PCM16_24K)
    await adapter.open(instructions="hi", reasoning_effort="low", tools=[])
    events = [e async for e in adapter.receive_events()]
    frame = events[0].payload["frame"]
    assert (frame.encoding, frame.sample_rate_hz, frame.payload) == ("audio/pcm", 24000, "AAAA")
