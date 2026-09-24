"""Only sustained speech interrupts Lelik: a line blip or an "uh-huh" under the threshold
leaves his reply playing (live, 2026-09-25: replies cut by 400 ms noises)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_MIN_SPEECH_S = 0.3


def _audio():
    frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload="AAAA", track="outbound")
    return E(type="audio_delta", payload={"frame": frame, "item_id": "i1"})


def _lelik_talking():
    # Pickup reply done, the caller asks, Lelik is mid-reply (response still active).
    return [E(type="response_created", payload={}), E(type="response_done", payload={}),
            E(type="turn_committed", payload={}), E(type="response_created", payload={}), _audio()]


async def _call(events):
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": []}
    clear = AsyncMock()

    async def inbound():
        await asyncio.sleep(0.8)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  barge_in_min_speech_s=_MIN_SPEECH_S)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=clear, playback=PlaybackTracker())
    return session, clear


@pytest.mark.asyncio
async def test_a_short_noise_does_not_interrupt_and_is_not_answered():
    async def events():
        for e in _lelik_talking():
            yield e
        yield E(type="speech_started", payload={})
        await asyncio.sleep(0.1)
        yield E(type="speech_stopped", payload={})
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session, clear = await _call(events)

    session.cancel_response.assert_not_awaited()
    clear.assert_not_awaited()
    session.truncate.assert_not_awaited()
    # pickup + the caller's question; the noise's committed turn starts no reply of its own
    assert session.request_response.await_count == 2


@pytest.mark.asyncio
async def test_sustained_speech_interrupts_after_the_threshold():
    async def events():
        for e in _lelik_talking():
            yield e
        yield E(type="speech_started", payload={})
        await asyncio.sleep(10)  # the caller keeps talking
        yield  # pragma: no cover

    session, clear = await _call(events)

    session.cancel_response.assert_awaited_once()
    clear.assert_awaited_once()
    session.truncate.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_speech_while_lelik_is_silent_is_answered_normally():
    async def events():
        yield E(type="response_created", payload={})
        yield E(type="response_done", payload={})
        yield E(type="speech_started", payload={})
        await asyncio.sleep(0.05)
        yield E(type="speech_stopped", payload={})
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    session, _ = await _call(events)

    assert session.request_response.await_count == 2  # pickup + the reply to "yes"
