"""VoiceSessionService opens the call's control link from the session config and closes it in teardown
(VOICE_CALL_CONTROL_CHANNEL_RFC §4.3): the page attached during the call hears `bye{call_ended}`, the digest is
forgotten, and teardown still reaches `submit_transcript`."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_playback_tracker import PlaybackTracker
from src.handlers.call_control_handler import CallControlHandler
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.call_control_hub import CallControlHub
from src.services.voice_session_service import VoiceSessionService
from tests.support.control_socket_fake import FakeControlSocket

TOKEN = "token-0001-invented"
TICKET = "ticket-0001-invented"


def _service(hub, config_extra, call_ends: asyncio.Event):
    session = AsyncMock()

    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session.receive_events = MagicMock(return_value=events())
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [], "call_kind": "web",
        **config_extra}

    async def inbound():
        await call_ends.wait()
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control_plane, alert_sink=AsyncMock(), control_hub=hub)
    call = service.handle_call(ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                               clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return call, control_plane


@pytest.mark.asyncio
async def test_the_page_attaches_during_the_call_and_hears_call_ended_at_teardown():
    hub, ends = CallControlHub(), asyncio.Event()
    call, control_plane = _service(hub, {"control_token_sha256": control_token_digest(TOKEN)}, ends)
    call_task = asyncio.ensure_future(call)
    page = FakeControlSocket()
    page.page_says({"type": "hello", "v": 1, "token": TOKEN})
    socket_task = asyncio.ensure_future(CallControlHandler(hub, link_wait_s=1.0).handle_connection(page))
    await asyncio.sleep(0.05)
    assert page.types() == ["welcome"] and len(hub) == 1

    ends.set()
    await asyncio.wait_for(call_task, timeout=2)
    await asyncio.wait_for(socket_task, timeout=2)

    assert page.frames()[-1] == {"type": "bye", "reason": "call_ended"} and page.closed
    assert len(hub) == 0
    control_plane.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_call_without_a_digest_opens_no_link():
    hub, ends = CallControlHub(), asyncio.Event()
    call, control_plane = _service(hub, {}, ends)
    call_task = asyncio.ensure_future(call)
    await asyncio.sleep(0.02)
    assert len(hub) == 0
    ends.set()
    await asyncio.wait_for(call_task, timeout=2)
    control_plane.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_service_without_a_hub_ignores_the_digest():
    ends = asyncio.Event()
    call, control_plane = _service(None, {"control_token_sha256": control_token_digest(TOKEN)}, ends)
    call_task = asyncio.ensure_future(call)
    await asyncio.sleep(0.02)
    ends.set()
    await asyncio.wait_for(call_task, timeout=2)
    control_plane.submit_transcript.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_provider_open_still_closes_the_link():
    hub = CallControlHub()
    failing = AsyncMock()
    failing.open.side_effect = RuntimeError("provider down")
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
        "control_token_sha256": control_token_digest(TOKEN)}
    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=failing),
                                  control_plane=control_plane, alert_sink=AsyncMock(), control_hub=hub)

    async def inbound():
        return
        yield  # pragma: no cover

    with pytest.raises(RuntimeError):
        await service.handle_call(ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                                  clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    assert len(hub) == 0
    control_plane.submit_transcript.assert_awaited_once()
