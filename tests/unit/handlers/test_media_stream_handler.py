import json
import pytest
from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_handle_connection_extracts_ticket_and_drives_session_service():
    messages = [
        {"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": "t1"}}},
        {"event": "media", "media": {"payload": "b64audio"}},
        {"event": "stop"},
    ]
    ws = FakeTwilioWs(messages)
    session_service = AsyncMock()

    handler = MediaStreamHandler(session_service=session_service)
    await handler.handle_connection(ws)

    session_service.handle_call.assert_awaited_once()
    call = session_service.handle_call.await_args
    assert call.kwargs["ticket"] == "t1"
