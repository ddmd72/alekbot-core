"""Speech in the greeting's first seconds never interrupts it: the browser's echo canceller is still
adapting, and live web calls heard Lelik's own greeting back as the caller, cut it and greeted
twice (2026-09-28). Past the guard, barge-in works as usual."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_MIN_SPEECH_S = 0.2
_GUARD_S = 0.5


def _audio():
    frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload="AAAA", track="outbound")
    return E(type="audio_delta", payload={"frame": frame, "item_id": "i1"})


async def _call(events, greeting_guard_s=_GUARD_S):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": []}
    clear = AsyncMock()

    async def inbound():
        await asyncio.sleep(1.2)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  barge_in_min_speech_s=_MIN_SPEECH_S, greeting_guard_s=greeting_guard_s)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=clear, playback=PlaybackTracker())
    return session, clear


def _echo_early_in_greeting():
    async def events():
        yield E(type="response_created", payload={})  # the pickup greeting
        yield _audio()
        yield E(type="speech_started", payload={})  # Lelik's own voice, echoed back
        await asyncio.sleep(0.3)  # longer than barge_in_min_speech_s
        yield E(type="speech_stopped", payload={})
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover
    return events


@pytest.mark.asyncio
async def test_sustained_speech_early_in_the_greeting_neither_interrupts_nor_is_answered():
    session, clear = await _call(_echo_early_in_greeting())

    session.cancel_response.assert_not_awaited()
    clear.assert_not_awaited()
    session.truncate.assert_not_awaited()
    assert session.request_response.await_count == 1  # the greeting only, no second greeting


@pytest.mark.asyncio
async def test_without_a_guard_the_same_speech_interrupts_the_greeting():
    session, _ = await _call(_echo_early_in_greeting(), greeting_guard_s=0.0)

    session.cancel_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_speech_after_the_guard_interrupts_the_greeting():
    async def events():
        yield E(type="response_created", payload={})
        yield _audio()
        await asyncio.sleep(_GUARD_S + 0.1)
        yield E(type="speech_started", payload={})
        await asyncio.sleep(10)  # the caller keeps talking
        yield  # pragma: no cover

    session, clear = await _call(events)

    session.cancel_response.assert_awaited_once()
    clear.assert_awaited_once()
