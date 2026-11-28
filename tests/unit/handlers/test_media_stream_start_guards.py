"""The phone path's socket is held to the same first-frame discipline as `/ctl`: a valid `start` within a timeout,
a cap on sockets still waiting for it, a well-formed ticket, one `start` per stream, no audio queued before it."""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock

from src.handlers.media_stream_handler import MediaStreamHandler

TICKET = "3f2c9d5e-0d4b-4b8e-9a51-1c2d3e4f5a6b"
OTHER_TICKET = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"


def _start(ticket=TICKET, sid="MZ1"):
    return {"event": "start", "start": {"streamSid": sid, "customParameters": {"ticket": ticket}}}


class ScriptedWs:
    def __init__(self, messages, then_hang=False):
        self._messages = list(messages)
        self._then_hang = then_hang
        self.sent = []
        self.closed_with = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return json.dumps(self._messages.pop(0))
        if self._then_hang:
            await asyncio.sleep(3600)
        raise StopAsyncIteration

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000):
        self.closed_with = code


@pytest.mark.asyncio
async def test_socket_with_no_first_frame_is_closed_after_the_timeout():
    ws = ScriptedWs([], then_hang=True)
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service, start_timeout_s=0.05)

    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    assert ws.closed_with == 1008
    session_service.handle_call.assert_not_called()


@pytest.mark.asyncio
async def test_other_frames_do_not_extend_the_start_deadline():
    class Drip(ScriptedWs):
        async def __anext__(self):
            await asyncio.sleep(0.02)
            return json.dumps({"event": "connected"})

    ws = Drip([])
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service, start_timeout_s=0.1)

    await asyncio.wait_for(handler.handle_connection(ws), timeout=2.0)

    assert ws.closed_with == 1008
    session_service.handle_call.assert_not_called()


@pytest.mark.asyncio
async def test_sockets_over_the_cap_are_refused_until_one_starts():
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service, start_timeout_s=5.0, max_unstarted=2)
    waiting = [ScriptedWs([], then_hang=True) for _ in range(2)]
    tasks = [asyncio.ensure_future(handler.handle_connection(ws)) for ws in waiting]
    await asyncio.sleep(0.05)

    refused = ScriptedWs([_start(), {"event": "stop"}])
    await handler.handle_connection(refused)

    assert refused.closed_with == 1013
    session_service.handle_call.assert_not_called()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_started_stream_frees_its_slot_under_the_cap():
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service, start_timeout_s=5.0, max_unstarted=1)

    await handler.handle_connection(ScriptedWs([_start(), {"event": "stop"}]))
    second = ScriptedWs([_start(OTHER_TICKET), {"event": "stop"}])
    await handler.handle_connection(second)

    assert session_service.handle_call.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket", ["t1", "", "not-a-uuid", TICKET.upper(), 7, None])
async def test_malformed_ticket_closes_without_a_control_plane_call(ticket):
    ws = ScriptedWs([_start(ticket), {"event": "stop"}])
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service)

    await handler.handle_connection(ws)

    assert ws.closed_with == 1008
    session_service.handle_call.assert_not_called()


@pytest.mark.asyncio
async def test_start_without_custom_parameters_closes_the_socket():
    ws = ScriptedWs([{"event": "start", "start": {"streamSid": "MZ1"}}])
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service)

    await handler.handle_connection(ws)

    assert ws.closed_with == 1008
    session_service.handle_call.assert_not_called()


@pytest.mark.asyncio
async def test_second_start_is_ignored():
    ws = ScriptedWs([_start(), _start(OTHER_TICKET, "MZ2"), {"event": "stop"}])
    session_service = AsyncMock()
    handler = MediaStreamHandler(session_service=session_service)

    await handler.handle_connection(ws)

    session_service.handle_call.assert_awaited_once()
    assert session_service.handle_call.await_args.kwargs["ticket"] == TICKET


@pytest.mark.asyncio
async def test_media_before_start_is_not_queued():
    consumed = []

    class Session:
        async def handle_call(self, ticket, inbound_audio, **_):
            async for frame in inbound_audio:
                consumed.append(frame.payload)

    ws = ScriptedWs([
        {"event": "media", "media": {"payload": "early"}},
        _start(),
        {"event": "media", "media": {"payload": "b64audio"}},
        {"event": "stop"},
    ])
    await MediaStreamHandler(session_service=Session()).handle_connection(ws)

    assert consumed == ["b64audio"]


@pytest.mark.asyncio
async def test_well_behaved_stream_runs_start_media_stop():
    ws = ScriptedWs([_start(), {"event": "media", "media": {"payload": "b64audio"}}, {"event": "stop"}])
    session_service = AsyncMock()

    await MediaStreamHandler(session_service=session_service).handle_connection(ws)

    session_service.handle_call.assert_awaited_once()
    assert session_service.handle_call.await_args.kwargs["ticket"] == TICKET
    assert ws.closed_with != 1008  # the session end closes it normally, never as a refusal
