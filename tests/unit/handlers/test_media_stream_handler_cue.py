"""Cue audio goes to Twilio as plain media: no mark, so it never counts as heard audio."""
import json

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.handlers.media_stream_handler import MediaStreamHandler


class FakeTwilioWs:
    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.sent: list[dict] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return json.dumps(self._messages.pop(0))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class CueSendingSessionService:
    def __init__(self):
        self.playback = None

    async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio,
                          playback, send_cue_audio):
        self.playback = playback
        await send_cue_audio(AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000,
                                        payload="Y3Vl", track="outbound"))
        async for _ in inbound_audio:
            pass


@pytest.mark.asyncio
async def test_cue_frame_is_sent_as_media_without_a_mark():
    ws = FakeTwilioWs([
        {"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": "t1"}}},
        {"event": "stop"},
    ])
    service = CueSendingSessionService()

    await MediaStreamHandler(session_service=service).handle_connection(ws)

    assert ws.sent == [{"event": "media", "streamSid": "MZ1", "media": {"payload": "Y3Vl"}}]
    assert service.playback.sent_bytes == 0
