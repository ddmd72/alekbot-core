"""Resumption in the relay's session loop (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2): the handle goes to open(), the resume
note is sent only to a session that resumed, and the call's handle is reported with the transcript."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

HANDLE = "conv-0001-invented"
_VOICE = {"provider": "xai", "turn_ownership": "provider", "voice": "rex", "reasoning_effort": "low"}


async def _call(config_extra, resumed=False, handle_event=HANDLE, caller_opening=None):
    session = AsyncMock()
    session.resumed = resumed

    async def events():
        if handle_event:
            yield E(type="resume_handle", payload={"handle": handle_event})
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "usr-1", "account_id": "acc-1", "tools": [],
        "call_kind": "web", "voice": _VOICE, **config_extra}

    async def inbound():
        await asyncio.sleep(0.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session), control_plane=control,
                                  alert_sink=AsyncMock(), caller_opening=caller_opening)
    await service.handle_call(ticket="ticket-0001-invented", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    return session, control


def _system_notes(session):
    return [text for role, text in (c.args for c in session.submit_message.await_args_list) if role == "system"]


@pytest.mark.asyncio
async def test_a_resume_config_passes_the_handle_to_open():
    session, _ = await _call({"resume": {"handle": HANDLE}}, resumed=True)

    assert session.open.await_args.kwargs["resume_handle"] == HANDLE


@pytest.mark.asyncio
async def test_a_fresh_call_opens_without_the_argument():
    session, _ = await _call({})

    assert "resume_handle" not in session.open.await_args.kwargs


@pytest.mark.asyncio
async def test_the_resume_note_replaces_the_pickup_note_when_resumed():
    session, _ = await _call({"resume": {"handle": HANDLE}}, resumed=True)

    notes = _system_notes(session)
    assert len(notes) == 1 and "dropped" in notes[0] and "Speak first" in notes[0]
    assert "connected to you from the web page" not in notes[0]
    session.request_response.assert_awaited()


@pytest.mark.asyncio
async def test_the_resume_note_is_bracketed_text_that_never_claims_the_systems_voice():
    session, _ = await _call({"resume": {"handle": HANDLE}}, resumed=True)

    note = _system_notes(session)[0]
    assert note.startswith("[") and note.endswith("]")
    assert "System:" not in note and "SYSTEM" not in note


@pytest.mark.asyncio
async def test_a_not_honoured_handle_opens_fresh_with_the_ordinary_pickup_note():
    session, _ = await _call({"resume": {"handle": HANDLE}}, resumed=False)

    notes = _system_notes(session)
    assert len(notes) == 1 and "connected to you from the web page" in notes[0]
    assert "dropped" not in notes[0]


_RELAY_VOICE = {"voice": {**_VOICE, "turn_ownership": "relay"}}
_OPENING = "Interpret between two languages."


@pytest.mark.asyncio
async def test_a_resumed_call_does_not_repeat_the_caller_opening():
    session, _ = await _call({"resume": {"handle": HANDLE}, **_RELAY_VOICE}, resumed=True, caller_opening=_OPENING)

    assert [c.args for c in session.submit_message.await_args_list if c.args[0] == "user"] == []


@pytest.mark.asyncio
async def test_a_not_honoured_resume_still_sends_the_caller_opening():
    session, _ = await _call({"resume": {"handle": HANDLE}, **_RELAY_VOICE}, resumed=False, caller_opening=_OPENING)

    assert [c.args for c in session.submit_message.await_args_list if c.args[0] == "user"] == [("user", _OPENING)]


@pytest.mark.asyncio
async def test_the_handle_event_is_reported_with_the_transcript_and_its_provider():
    _, control = await _call({})

    kwargs = control.submit_transcript.await_args.kwargs
    assert kwargs["resume_handle"] == HANDLE and kwargs["resume_provider"] == "xai"


@pytest.mark.asyncio
async def test_a_provider_without_a_handle_reports_as_before():
    _, control = await _call({}, handle_event=None)

    assert "resume_handle" not in control.submit_transcript.await_args.kwargs
