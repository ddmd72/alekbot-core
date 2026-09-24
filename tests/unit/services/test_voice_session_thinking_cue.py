"""The thinking cue fills a silent gap while Lelik thinks or waits, and never delays his reply."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_playback_tracker import PlaybackTracker
from src.ports.realtime_session_port import RealtimeSessionEvent as E
from src.services.voice_session_service import VoiceSessionService

_TOOLS = [{"name": "delegate_to_specialist", "description": "d", "parameters": {"type": "object"}}]
# 50 ms of distinct μ-law bytes: long enough to wrap around inside one test.
_CUE = bytes(range(1, 201)) * 2
_GRACE_S = 0.05


def _audio(item_id="i1"):
    frame = AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload="AAAA", track="outbound")
    return E(type="audio_delta", payload={"frame": frame, "item_id": item_id})


def _opening():
    return [E(type="response_created", payload={}), E(type="response_done", payload={})]


def _tool_call():
    return E(type="tool_call", payload={"call_id": "c1", "name": "delegate_to_specialist",
                                        "arguments": json.dumps({"intent": "search_web", "query": "q"})})


async def _hold():
    await asyncio.sleep(10)


async def _call(events, cue=_CUE, delegate=None, playback=None):
    log = []
    session = AsyncMock()
    session.receive_events = MagicMock(return_value=events())
    control = AsyncMock()
    control.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1", "tools": _TOOLS}
    if delegate is not None:
        control.delegate.side_effect = delegate

    async def send_outbound(frame):
        log.append(("audio", frame.payload))

    async def send_cue(frame):
        log.append(("cue", frame.payload))

    async def clear():
        log.append(("clear", None))

    async def inbound():
        await asyncio.sleep(0.6)
        return
        yield  # pragma: no cover

    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=session),
                                  control_plane=control, alert_sink=AsyncMock(),
                                  thinking_cue=cue, cue_grace_s=_GRACE_S)
    await service.handle_call(ticket="t1", inbound_audio=inbound(), send_outbound_audio=send_outbound,
                              clear_outbound_audio=clear, playback=playback or PlaybackTracker(),
                              send_cue_audio=send_cue)
    return log


def _kinds(log):
    return [kind for kind, _ in log]


@pytest.mark.asyncio
async def test_cue_fills_a_slow_reply_and_is_cleared_before_the_first_word():
    async def events():
        for e in _opening():
            yield e
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(0.3)  # Lelik thinks
        yield _audio()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events)

    kinds = _kinds(log)
    first_audio = kinds.index("audio")
    assert "cue" in kinds[:first_audio]
    assert kinds[first_audio - 1] == "clear"  # the cue's queued tail never plays over the reply
    assert "cue" not in kinds[first_audio:]


@pytest.mark.asyncio
async def test_a_fast_reply_gets_no_cue():
    async def events():
        for e in _opening():
            yield e
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(0.01)
        yield _audio()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events)

    assert "cue" not in _kinds(log)
    assert "clear" not in _kinds(log)


@pytest.mark.asyncio
async def test_the_pickup_greeting_gets_no_cue():
    async def events():
        yield E(type="response_created", payload={})
        await asyncio.sleep(0.3)  # the greeting takes a while to start
        yield _audio()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events)

    assert "cue" not in _kinds(log)


@pytest.mark.asyncio
async def test_cue_fills_a_silent_delegation_wait_and_stops_when_the_caller_speaks():
    async def never(**_):
        await asyncio.sleep(10)

    async def events():
        for e in _opening():
            yield e
        yield E(type="turn_committed", payload={})
        yield E(type="response_created", payload={})
        yield _tool_call()
        yield E(type="response_done", payload={})
        await asyncio.sleep(0.25)  # nobody speaks while the specialist works
        yield E(type="speech_started", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events, delegate=never)

    kinds = _kinds(log)
    assert "cue" in kinds
    # The call runs ~0.6 s; a cue that kept going after the caller spoke would send ~27 frames.
    # Stopped at speech: at most the ~0.25 s silent window plus the lead (20 ms per frame).
    assert kinds.count("cue") * 20 <= 250 + 150


@pytest.mark.asyncio
async def test_cue_frames_are_20ms_slices_of_the_clip_paced_in_real_time_and_untracked():
    playback = PlaybackTracker()

    async def events():
        for e in _opening():
            yield e
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(0.4)
        yield _audio()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events, playback=playback)

    cue = [base64.b64decode(payload) for kind, payload in log if kind == "cue"]
    assert cue and all(len(chunk) == 160 for chunk in cue)
    sent = b"".join(cue)
    assert sent == (_CUE * (len(sent) // len(_CUE) + 1))[:len(sent)]  # the clip, looped from its start
    # Real-time pacing: roughly (0.4 s - grace) of audio plus a short lead, never the whole wait at once.
    assert len(sent) <= (400 - int(_GRACE_S * 1000) + 150) * 8
    assert playback.sent_bytes == 0  # the cue never enters the heard-audio accounting


@pytest.mark.asyncio
async def test_no_clip_configured_means_no_cue():
    async def events():
        for e in _opening():
            yield e
        yield E(type="turn_committed", payload={})
        await asyncio.sleep(0.3)
        yield _audio()
        yield E(type="response_done", payload={})
        await _hold()
        yield  # pragma: no cover

    log = await _call(events, cue=b"")

    assert "cue" not in _kinds(log)
