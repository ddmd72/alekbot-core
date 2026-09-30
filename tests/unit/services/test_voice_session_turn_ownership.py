"""Per-call provider spec and turn ownership (VOICE_MULTI_PROVIDER_RFC §4.2/§4.3). One relay serves
users on different providers, so both come from each call's session config, not the constructor."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.services.voice_session_service as voice_session_module
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import LEGACY_VOICE_SESSION, VOICE_PROVIDER_PROFILES
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.services.voice_session_service import VoiceSessionService

# Enough persona sections for build_persona_anchor to produce an anchor under RELAY ownership.
_PERSONA = "identity {\n  role: x\n}\nvoice {\n  tone: y\n}\nhumor_engine {\n  style: z\n}\n"
_XAI = VOICE_PROVIDER_PROFILES["xai"].session
_OPENAI = VOICE_PROVIDER_PROFILES["openai"].session
_OPENING = "Welcome the user."


async def _idle(seconds: float):
    await asyncio.sleep(seconds)
    return
    yield  # pragma: no cover - makes this an async generator


async def _no_events():
    return
    yield  # pragma: no cover


def _service(realtime_session, voice=None, factory=None, **kwargs):
    control_plane = AsyncMock()
    config = {"instructions": _PERSONA, "user_id": "u1", "account_id": "a1"}
    if voice is not None:
        config["voice"] = voice.to_dict()
    control_plane.fetch_session_config.return_value = config
    return VoiceSessionService(
        realtime_session_factory=factory or MagicMock(return_value=realtime_session),
        control_plane=control_plane, alert_sink=AsyncMock(), **kwargs,
    )


async def _call(service, realtime_session, events, seconds=0.0, clear=None):
    realtime_session.receive_events = MagicMock(return_value=events())
    await service.handle_call(
        ticket="t1", inbound_audio=_idle(seconds), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear or AsyncMock(), playback=PlaybackTracker(),
    )


def _notes(realtime_session):
    return [c.args[1] for c in realtime_session.submit_message.await_args_list]


class TestSpecPerCall:
    @pytest.mark.asyncio
    async def test_the_factory_gets_the_calls_spec_and_open_gets_its_effort(self):
        realtime_session = AsyncMock()
        factory = MagicMock(return_value=realtime_session)

        await _call(_service(realtime_session, voice=_XAI, factory=factory), realtime_session, _no_events)

        factory.assert_called_once_with(_XAI)
        assert realtime_session.open.await_args.kwargs["reasoning_effort"] == "high"

    @pytest.mark.asyncio
    async def test_a_config_without_a_voice_spec_runs_the_legacy_openai_session(self):
        realtime_session = AsyncMock()
        factory = MagicMock(return_value=realtime_session)

        await _call(_service(realtime_session, factory=factory), realtime_session, _no_events)

        factory.assert_called_once_with(LEGACY_VOICE_SESSION)
        assert realtime_session.open.await_args.kwargs["reasoning_effort"] == "medium"

    @pytest.mark.asyncio
    async def test_a_constructor_effort_overrides_the_spec(self):
        realtime_session = AsyncMock()

        await _call(_service(realtime_session, voice=_XAI, reasoning_effort="none"), realtime_session, _no_events)

        assert realtime_session.open.await_args.kwargs["reasoning_effort"] == "none"


class TestRelayOwnedTurns:
    @pytest.mark.asyncio
    async def test_greeting_is_anchored_after_the_caller_opening(self):
        realtime_session = AsyncMock()

        await _call(_service(realtime_session, voice=_OPENAI, caller_opening=_OPENING), realtime_session, _no_events)

        notes = _notes(realtime_session)
        assert notes[0] == _OPENING
        assert any("identity" in note for note in notes)

    @pytest.mark.asyncio
    async def test_a_committed_turn_starts_a_reply(self):
        realtime_session = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="response_done", payload={})
            yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "u1"})

        await _call(_service(realtime_session, voice=_OPENAI), realtime_session, events)

        assert realtime_session.request_response.await_count == 2  # greeting + the turn


class TestProviderOwnedTurns:
    @pytest.mark.asyncio
    async def test_greeting_has_no_anchor_and_no_caller_opening(self):
        realtime_session = AsyncMock()

        await _call(_service(realtime_session, voice=_XAI, caller_opening=_OPENING), realtime_session, _no_events)

        assert _notes(realtime_session) == ["[The caller has just picked up the phone you called. Speak first.]"]
        realtime_session.request_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_committed_turn_does_not_start_a_reply(self):
        realtime_session = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="response_done", payload={})
            yield RealtimeSessionEvent(type="speech_started", payload={})
            yield RealtimeSessionEvent(type="speech_stopped", payload={})
            yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "u1"})

        await _call(_service(realtime_session, voice=_XAI), realtime_session, events)

        realtime_session.request_response.assert_awaited_once()  # the greeting only

    @pytest.mark.asyncio
    async def test_caller_over_a_reply_drops_queued_audio_without_cancel_or_truncate(self):
        realtime_session = AsyncMock()
        clear = AsyncMock()

        async def events():
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="speech_started", payload={})

        await _call(_service(realtime_session, voice=_XAI, barge_in_min_speech_s=1.0), realtime_session, events,
                    clear=clear)

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

        await _call(_service(realtime_session, voice=_XAI, silence_timeout_s=0.05), realtime_session, events,
                    seconds=0.3)

        assert not any("silent" in note for note in _notes(realtime_session))
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

        await _call(_service(realtime_session, voice=_XAI), realtime_session, events, clear=clear)

        clear.assert_awaited_once()
