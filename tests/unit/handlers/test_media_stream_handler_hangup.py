"""When the session ends on its own, the relay closes the media stream: Twilio's <Connect>
then ends, and with no TwiML after it the call hangs up."""
import json

import pytest

from src.handlers.media_stream_handler import MediaStreamHandler

# Tickets are minted as `str(uuid.uuid4())`; the handler refuses any other shape.
TICKET = "3f2c9d5e-0d4b-4b8e-9a51-1c2d3e4f5a6b"


class ClosableTwilioWs:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return json.dumps(self._messages.pop(0))

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


class EndsAtOnceSessionService:
    async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio,
                          playback, send_cue_audio=None):
        return  # e.g. the silence watchdog hung up


@pytest.mark.asyncio
async def test_stream_is_closed_when_the_session_ends():
    ws = ClosableTwilioWs([{"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": TICKET}}}])

    await MediaStreamHandler(session_service=EndsAtOnceSessionService()).handle_connection(ws)

    assert ws.closed
