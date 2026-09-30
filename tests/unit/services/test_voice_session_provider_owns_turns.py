"""provider_owns_turns: the bare-Grok experiment (decisions/voice_xai_protocol_probe.md).
The provider starts replies and stops when talked over; the relay adds nothing but the greeting,
delegation answers, and dropping audio it has already queued."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_session_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.voice_session_service import VoiceSessionService

# Enough persona sections for build_persona_anchor to produce an anchor in the default mode.
_PERSONA = "identity {\n  role: x\n}\nvoice {\n  tone: y\n}\nhumor_engine {\n  style: z\n}\n"


async def _idle(seconds: float):
    await asyncio.sleep(seconds)
    return
    yield  # pragma: no cover - makes this an async generator


def _service(realtime_session, provider_owns_turns: bool, **kwargs) -> VoiceSessionService:
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": _PERSONA, "user_id": "u1", "account_id": "a1"}
    return VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
        provider_owns_turns=provider_owns_turns,
        **kwargs,
    )


async def _call(service, realtime_session, events, seconds=0.0, clear=None, playback=None):
    realtime_session.receive_events = MagicMock(return_value=events())
    await service.handle_call(
        ticket="t1", inbound_audio=_idle(seconds), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear or AsyncMock(), playback=playback or PlaybackTracker(),
    )


class TestProviderOwnsTurns:
    @pytest.mark.asyncio
    async def test_default_mode_anchors_the_greeting(self):
        realtime_session = AsyncMock()

        async def events():
            return
            yield  # pragma: no cover

        await _call(_service(realtime_session, provider_owns_turns=False), realtime_session, events)

        notes = [c.args[1] for c in realtime_session.submit_message.await_args_list]
        assert any("identity" in note for note in notes)

    @pytest.mark.asyncio
    async def test_greeting_is_started_without_a_persona_anchor(self):
        realtime_session = AsyncMock()

        async def events():
            return
            yield  # pragma: no cover

        await _call(_service(realtime_session, provider_owns_turns=True), realtime_session, events)

        notes = [c.args[1] for c in realtime_session.submit_message.await_args_list]
        assert notes == ["[The caller has just picked up the phone you called. Speak first.]"]
        realtime_session.request_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_committed_turn_does_not_start_a_reply(self):
        realtime_session = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})  # greeting
            yield RealtimeSessionEvent(type="response_done", payload={})
            yield RealtimeSessionEvent(type="speech_started", payload={})
            yield RealtimeSessionEvent(type="speech_stopped", payload={})
            yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "u1"})

        await _call(_service(realtime_session, provider_owns_turns=True), realtime_session, events)

        realtime_session.request_response.assert_awaited_once()  # the greeting only

    @pytest.mark.asyncio
    async def test_caller_over_a_reply_drops_queued_audio_without_cancel_or_truncate(self):
        realtime_session = AsyncMock()
        clear = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="speech_started", payload={})

        await _call(_service(realtime_session, provider_owns_turns=True, barge_in_min_speech_s=1.0),
                    realtime_session, events, clear=clear)

        clear.assert_awaited_once()
        realtime_session.cancel_response.assert_not_awaited()
        realtime_session.truncate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_silence_notes(self, monkeypatch):
        monkeypatch.setattr(voice_session_module, "_WATCHDOG_TICK_S", 0.01)
        realtime_session = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="response_done", payload={})
            await asyncio.sleep(10)
            yield  # pragma: no cover

        await _call(_service(realtime_session, provider_owns_turns=True, silence_timeout_s=0.05),
                    realtime_session, events, seconds=0.3)

        notes = [c.args[1] for c in realtime_session.submit_message.await_args_list]
        assert not any("silent" in note for note in notes)
        realtime_session.request_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_provider_started_reply_is_tracked_as_active(self):
        realtime_session = AsyncMock()
        clear = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})  # greeting
            yield RealtimeSessionEvent(type="response_done", payload={})
            yield RealtimeSessionEvent(type="response_created", payload={})  # the provider's own reply
            yield RealtimeSessionEvent(type="speech_started", payload={})

        await _call(_service(realtime_session, provider_owns_turns=True), realtime_session, events, clear=clear)

        clear.assert_awaited_once()
