import asyncio
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


@pytest.mark.asyncio
async def test_handle_connection_cleans_up_on_clean_close_without_stop_event():
    """Regression test for the clean-close leak: websockets==15.0.1's
    Connection.__aiter__ swallows ConnectionClosedOK internally and just
    returns (no exception, no "stop" message) — the `async for raw in ws:`
    loop simply ends. This fakes exactly that: a WebSocket that yields
    "start" + one "media" frame, then stops iterating with no "stop" event
    and no exception. Before the fix, handle_connection returned without
    ever pushing the None sentinel onto inbound_queue or awaiting call_task,
    so VoiceSessionService's consumption of inbound_audio (here, the
    FakeSessionService.handle_call below) blocked forever. asyncio.wait_for
    turns that hang into a clear test failure instead of a silent timeout.
    """
    messages = [
        {"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": "t1"}}},
        {"event": "media", "media": {"payload": "b64audio"}},
    ]
    ws = FakeTwilioWs(messages)

    consumed_frames = []
    task_holder: dict = {}

    class FakeSessionService:
        async def handle_call(self, ticket, inbound_audio, send_outbound_audio):
            task_holder["task"] = asyncio.current_task()
            # This loop only terminates once handle_connection pushes the
            # None sentinel onto inbound_queue - if it never does (the bug),
            # this hangs forever and the outer wait_for below times out.
            async for frame in inbound_audio:
                consumed_frames.append(frame)

    handler = MediaStreamHandler(session_service=FakeSessionService())

    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    # handle_connection's finally awaits call_task before returning, so
    # reaching here already proves call_task completed - assert explicitly too.
    assert task_holder["task"].done()
    assert len(consumed_frames) == 1
    assert consumed_frames[0].payload == "b64audio"


@pytest.mark.asyncio
async def test_clear_outbound_audio_sends_twilio_clear_with_real_stream_sid():
    """Barge-in's Twilio half (I5): the handler hands VoiceSessionService a
    callback that drops everything still queued for playback. Twilio's wire
    shape is `{"event": "clear", "streamSid": ...}` (validated live in
    scripts/voice/test_mulaw_relay_poc.py:168) and the streamSid must be the
    real one captured from the `start` event, not the ticket."""
    messages = [{"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": "t1"}}}]
    ws = FakeTwilioWs(messages)
    captured: dict = {}

    class FakeSessionService:
        async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio):
            captured["clear"] = clear_outbound_audio
            await clear_outbound_audio()

    handler = MediaStreamHandler(session_service=FakeSessionService())
    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    assert ws.sent == [{"event": "clear", "streamSid": "MZ1"}]


@pytest.mark.asyncio
async def test_no_clear_frame_is_sent_when_no_start_event_ever_arrives():
    """A connection that closes before Twilio's `start` event must write
    nothing to the socket. Note what this does and does not prove: the
    `stream_sid is None` guard inside clear_outbound_audio is defensive parity
    with send_outbound's identical guard and is NOT reachable through the
    public flow - the callback is only handed to handle_call from inside the
    `start` branch, after stream_sid is assigned. This asserts the observable
    contract (no stray frames pre-start); the guard itself stays as a
    belt-and-braces mirror of its sibling."""
    ws = FakeTwilioWs([{"event": "media", "media": {"payload": "b64audio"}}])
    handler = MediaStreamHandler(session_service=AsyncMock())

    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    assert ws.sent == []


@pytest.mark.asyncio
async def test_clear_outbound_audio_is_reusable_across_repeated_barge_ins():
    """One call can be interrupted many times, so the callback is not
    single-use - each invocation emits its own clear frame."""
    messages = [{"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": "t1"}}}]
    ws = FakeTwilioWs(messages)

    class FakeSessionService:
        async def handle_call(self, ticket, inbound_audio, send_outbound_audio, clear_outbound_audio):
            await clear_outbound_audio()
            await clear_outbound_audio()

    handler = MediaStreamHandler(session_service=FakeSessionService())
    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    assert ws.sent == [
        {"event": "clear", "streamSid": "MZ1"},
        {"event": "clear", "streamSid": "MZ1"},
    ]
