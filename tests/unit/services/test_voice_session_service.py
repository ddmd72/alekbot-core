import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.voice_session_service import VoiceSessionService
from src.domain.voice_audio_frame import AudioFrame
from src.ports.realtime_session_port import RealtimeSessionEvent
from src.domain.voice_playback_tracker import PlaybackTracker


async def _frames(*frames):
    for f in frames:
        yield f


@pytest.mark.asyncio
async def test_handle_call_relays_audio_and_flushes_buffer_on_close():
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1",
    }
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="user_transcript", payload={"text": "what's my favorite color"})
        yield RealtimeSessionEvent(
            type="audio_delta",
            payload={"frame": AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload="b64", track="outbound")},
        )
        yield RealtimeSessionEvent(type="model_transcript", payload={"text": "Your favorite color is teal."})
        yield RealtimeSessionEvent(
            type="response_done",
            payload={"usage": {"input_tokens": 10, "output_tokens": 5}, "model": "gpt-realtime-2.1"},
        )

    realtime_session.receive_events = MagicMock(return_value=events())
    session_factory = MagicMock(return_value=realtime_session)

    outbound_sent = []
    service = VoiceSessionService(realtime_session_factory=session_factory, control_plane=control_plane, alert_sink=AsyncMock())

    inbound = _frames(AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload="in1", track="inbound"))
    # send_outbound_audio is Callable[[AudioFrame], Awaitable[None]] - must be awaitable,
    # unlike a plain list.append.
    await service.handle_call(
        ticket="t1",
        inbound_audio=inbound,
        send_outbound_audio=AsyncMock(side_effect=outbound_sent.append),
        clear_outbound_audio=AsyncMock(),
        playback=PlaybackTracker(),
    )

    control_plane.fetch_session_config.assert_awaited_once_with("t1")
    realtime_session.open.assert_awaited_once()
    open_kwargs = realtime_session.open.await_args.kwargs
    assert open_kwargs["instructions"] == "you are Lelik"
    assert open_kwargs["reasoning_effort"] == "medium"

    realtime_session.send_audio.assert_awaited_once()
    assert len(outbound_sent) == 1

    control_plane.submit_transcript.assert_awaited_once()
    submit_kwargs = control_plane.submit_transcript.await_args.kwargs
    assert submit_kwargs["call_id"] == "t1"
    assert submit_kwargs["user_id"] == "u1"
    assert submit_kwargs["account_id"] == "a1"
    submitted_buffer = submit_kwargs["buffer"]
    assert submitted_buffer.usage_by_model["gpt-realtime-2.1"] == {"input_tokens": 10, "output_tokens": 5}
    assert len(submitted_buffer.turns) == 1
    assert submitted_buffer.turns[0].request_text == "what's my favorite color"
    assert submitted_buffer.turns[0].response_text == "Your favorite color is teal."
    realtime_session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_call_alerts_and_flushes_on_provider_error():
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="error", payload={"message": "socket dropped"})

    realtime_session.receive_events = MagicMock(return_value=events())
    alert_sink = AsyncMock()
    service = VoiceSessionService(realtime_session_factory=MagicMock(return_value=realtime_session), control_plane=control_plane, alert_sink=alert_sink)

    await service.handle_call(
        ticket="t1",
        inbound_audio=_frames(),
        send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(),
        playback=PlaybackTracker(),
    )

    alert_sink.post.assert_awaited_once()
    assert "socket dropped" in alert_sink.post.await_args.args[0]
    control_plane.submit_transcript.assert_awaited_once()  # a dropped call still summarizes (RFC §4.9)
    submit_kwargs = control_plane.submit_transcript.await_args.kwargs
    assert submit_kwargs["call_id"] == "t1"
    assert submit_kwargs["user_id"] == "u1"
    assert submit_kwargs["account_id"] == "a1"


@pytest.mark.asyncio
async def test_handle_call_still_submits_transcript_and_closes_when_open_fails():
    # A transient provider connection failure in session.open() must still reach
    # close()/submit_transcript() - otherwise the relay's one-call-per-user marker
    # only releases via TTL instead of immediately, and the failure leaves no
    # record on the main-service side.
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()
    realtime_session.open.side_effect = ConnectionError("provider unreachable")
    service = VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )

    with pytest.raises(ConnectionError):
        await service.handle_call(
            ticket="t1",
            inbound_audio=_frames(),
            send_outbound_audio=AsyncMock(),
            clear_outbound_audio=AsyncMock(),
            playback=PlaybackTracker(),
        )

    realtime_session.close.assert_awaited_once()
    control_plane.submit_transcript.assert_awaited_once()
    submit_kwargs = control_plane.submit_transcript.await_args.kwargs
    assert submit_kwargs["call_id"] == "t1"
    assert submit_kwargs["user_id"] == "u1"
    assert submit_kwargs["account_id"] == "a1"
    assert submit_kwargs["buffer"].turns == []


@pytest.mark.asyncio
async def test_speech_started_mid_response_clears_twilio_buffer_and_cancels():
    """Barge-in (I5), mirroring the mechanism validated live in
    scripts/voice/test_mulaw_relay_poc.py:158-170. Cancelling the provider's
    response alone is not enough: audio already handed to Twilio keeps playing
    out over the caller, so BOTH halves must fire."""
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    service = VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )
    clear_outbound = AsyncMock()

    await service.handle_call(
        ticket="t1",
        inbound_audio=_frames(),
        send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear_outbound,
        playback=PlaybackTracker(),
    )

    clear_outbound.assert_awaited_once()
    realtime_session.cancel_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_speech_started_with_no_active_response_does_not_cancel():
    """The response_active guard is load-bearing, not defensive tidiness: the
    POC documents that a `response.cancel` with nothing in flight raises a
    provider-side error, which this service turns into an alert + an ended
    call. A caller who speaks first (before Lelik ever answers) must therefore
    cancel nothing."""
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    service = VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )
    clear_outbound = AsyncMock()

    await service.handle_call(
        ticket="t1",
        inbound_audio=_frames(),
        send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear_outbound,
        playback=PlaybackTracker(),
    )

    clear_outbound.assert_not_awaited()
    realtime_session.cancel_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_speech_started_after_response_done_does_not_cancel():
    """response_done must clear response_active, or the next utterance in a
    normal (uninterrupted) exchange would fire a spurious response.cancel
    against a finished response - the exact provider-side error the guard
    exists to avoid."""
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="response_done", payload={"usage": {}, "model": "gpt-realtime-2.1"})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    service = VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )
    clear_outbound = AsyncMock()

    await service.handle_call(
        ticket="t1",
        inbound_audio=_frames(),
        send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear_outbound,
        playback=PlaybackTracker(),
    )

    clear_outbound.assert_not_awaited()
    realtime_session.cancel_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_barge_in_within_one_response_cancels_only_once():
    """A caller talking in bursts produces several speech_started events inside
    one response. The first consumes the active response; the rest must be
    no-ops, again because a second response.cancel would hit nothing in flight."""
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    service = VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )
    clear_outbound = AsyncMock()

    await service.handle_call(
        ticket="t1",
        inbound_audio=_frames(),
        send_outbound_audio=AsyncMock(),
        clear_outbound_audio=clear_outbound,
        playback=PlaybackTracker(),
    )

    assert clear_outbound.await_count == 1
    assert realtime_session.cancel_response.await_count == 1


# =============================================================================
# Barge-in truncation + silence watchdog (decisions/lelik_warm_context.md, playbook 1.3)
# =============================================================================

import asyncio  # noqa: E402
import base64  # noqa: E402

import src.services.voice_session_service as voice_session_module  # noqa: E402


def _chunk(ms: int) -> AudioFrame:
    payload = base64.b64encode(b"\xff" * (ms * 8)).decode()
    return AudioFrame(encoding="audio/pcmu", sample_rate_hz=8000, payload=payload, track="outbound")


def _service(realtime_session, silence_timeout_s=8.0):
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {"instructions": "hi", "user_id": "u1", "account_id": "a1"}
    return VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
        silence_timeout_s=silence_timeout_s,
    )


@pytest.mark.asyncio
async def test_barge_in_reads_heard_audio_then_clears_cancels_and_truncates_in_order():
    """Heard audio is read BEFORE clear (Twilio echoes dropped marks after a clear),
    and the item is cut to what the caller heard so the model resumes from there."""
    playback = PlaybackTracker()
    calls = []
    realtime_session = AsyncMock()
    realtime_session.cancel_response.side_effect = lambda: calls.append("cancel")
    realtime_session.truncate.side_effect = lambda item_id, ms: calls.append(("truncate", item_id, ms))

    async def send(frame):
        playback.record_sent(frame.payload)

    async def clear():
        calls.append("clear")
        playback.record_played(str(playback.sent_bytes))  # Twilio echoes every dropped mark

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(1000), "item_id": "item_1"})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(1000), "item_id": "item_1"})
        playback.record_played(str(600 * 8))  # caller heard 600 ms of 2000
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=send,
        clear_outbound_audio=clear, playback=playback,
    )

    assert calls == ["clear", "cancel", ("truncate", "item_1", 600)]


@pytest.mark.asyncio
async def test_truncation_counts_from_the_start_of_the_interrupted_item():
    playback = PlaybackTracker()
    realtime_session = AsyncMock()

    async def send(frame):
        playback.record_sent(frame.payload)

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(1000), "item_id": "item_1"})
        yield RealtimeSessionEvent(type="response_done", payload={})
        playback.record_played(str(1000 * 8))
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(1000), "item_id": "item_2"})
        playback.record_played(str(1250 * 8))
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=send,
        clear_outbound_audio=AsyncMock(), playback=playback,
    )

    realtime_session.truncate.assert_awaited_once_with("item_2", 250)


@pytest.mark.asyncio
async def test_barge_in_before_any_audio_cancels_without_truncating():
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=PlaybackTracker(),
    )

    realtime_session.cancel_response.assert_awaited_once()
    realtime_session.truncate.assert_not_awaited()


async def _run_open_call(realtime_session, events, playback, seconds, silence_timeout_s):
    """Keep the call open for `seconds` (inbound stream idles), then end it."""
    async def inbound():
        await asyncio.sleep(seconds)
        return
        yield  # pragma: no cover - makes this an async generator

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session, silence_timeout_s=silence_timeout_s).handle_call(
        ticket="t1", inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=playback,
    )


@pytest.mark.asyncio
async def test_silence_watchdog_prompts_once_after_timeout(monkeypatch):
    monkeypatch.setattr(voice_session_module, "_WATCHDOG_TICK_S", 0.01)
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    await _run_open_call(realtime_session, events, PlaybackTracker(), seconds=0.3, silence_timeout_s=0.05)

    silence = [c.args for c in realtime_session.submit_message.await_args_list if "silent" in c.args[1]]
    assert silence == [("system", "[The caller has been silent for 0 seconds.]")]
    assert realtime_session.request_response.await_count == 2  # opening line + silence check


@pytest.mark.asyncio
async def test_silence_watchdog_rearms_after_the_caller_speaks(monkeypatch):
    monkeypatch.setattr(voice_session_module, "_WATCHDOG_TICK_S", 0.01)
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        await asyncio.sleep(0.15)
        yield RealtimeSessionEvent(type="speech_started", payload={})
        yield RealtimeSessionEvent(type="speech_stopped", payload={})
        await asyncio.sleep(10)

    await _run_open_call(realtime_session, events, PlaybackTracker(), seconds=0.4, silence_timeout_s=0.05)

    silence = [c for c in realtime_session.submit_message.await_args_list if "silent" in c.args[1]]
    assert len(silence) == 2


@pytest.mark.asyncio
async def test_silence_watchdog_waits_while_lelik_audio_is_still_playing(monkeypatch):
    monkeypatch.setattr(voice_session_module, "_WATCHDOG_TICK_S", 0.01)
    realtime_session = AsyncMock()
    playback = PlaybackTracker()
    playback.record_sent(_chunk(5000).payload)  # sent, never acknowledged as played

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        await asyncio.sleep(10)
        yield  # pragma: no cover

    await _run_open_call(realtime_session, events, playback, seconds=0.2, silence_timeout_s=0.05)

    assert not [c for c in realtime_session.submit_message.await_args_list if "silent" in c.args[1]]


@pytest.mark.asyncio
async def test_silence_watchdog_waits_while_a_response_is_active(monkeypatch):
    monkeypatch.setattr(voice_session_module, "_WATCHDOG_TICK_S", 0.01)
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        yield RealtimeSessionEvent(type="response_created", payload={})
        await asyncio.sleep(10)

    await _run_open_call(realtime_session, events, PlaybackTracker(), seconds=0.2, silence_timeout_s=0.05)

    assert not [c for c in realtime_session.submit_message.await_args_list if "silent" in c.args[1]]


@pytest.mark.asyncio
async def test_barge_in_while_a_finished_reply_is_still_playing_clears_and_truncates_without_cancel():
    """First live call: the provider finished generating seconds before Twilio finished
    playing, response_active was already False, and 'stop' was never heard. Barge-in
    must follow playback; nothing is left to cancel, so no response.cancel."""
    playback = PlaybackTracker()
    calls = []
    realtime_session = AsyncMock()
    realtime_session.cancel_response.side_effect = lambda: calls.append("cancel")
    realtime_session.truncate.side_effect = lambda item_id, ms: calls.append(("truncate", item_id, ms))

    async def send(frame):
        playback.record_sent(frame.payload)

    async def clear():
        calls.append("clear")

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(4000), "item_id": "item_1"})
        yield RealtimeSessionEvent(type="response_done", payload={})
        playback.record_played(str(1500 * 8))  # 1.5 s of 4 s heard so far
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=send,
        clear_outbound_audio=clear, playback=playback,
    )

    assert calls == ["clear", ("truncate", "item_1", 1500)]


@pytest.mark.asyncio
async def test_speech_after_the_reply_has_fully_played_is_not_a_barge_in():
    playback = PlaybackTracker()
    realtime_session = AsyncMock()
    clear = AsyncMock()

    async def send(frame):
        playback.record_sent(frame.payload)

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(1000), "item_id": "item_1"})
        yield RealtimeSessionEvent(type="response_done", payload={})
        playback.record_played(str(playback.sent_bytes))
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=send,
        clear_outbound_audio=clear, playback=playback,
    )

    clear.assert_not_awaited()
    realtime_session.truncate.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_item_is_truncated_once_even_if_the_caller_keeps_talking_over_it():
    playback = PlaybackTracker()
    realtime_session = AsyncMock()

    async def send(frame):
        playback.record_sent(frame.payload)

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="audio_delta", payload={"frame": _chunk(4000), "item_id": "item_1"})
        yield RealtimeSessionEvent(type="response_done", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})
        yield RealtimeSessionEvent(type="speech_stopped", payload={})
        yield RealtimeSessionEvent(type="speech_started", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _service(realtime_session).handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=send,
        clear_outbound_audio=AsyncMock(), playback=playback,
    )

    realtime_session.truncate.assert_awaited_once()


# =============================================================================
# Per-turn persona anchor (create_response off: the relay starts every reply)
# =============================================================================

_PERSONA_INSTRUCTIONS = "identity {\n x\n}\nvoice {\n y\n}\nhumor_engine {\n z\n}\nspoken_delivery {\n w\n}"


def _service_with(realtime_session, instructions):
    control_plane = AsyncMock()
    control_plane.fetch_session_config.return_value = {
        "instructions": instructions, "user_id": "u1", "account_id": "a1",
    }
    return VoiceSessionService(
        realtime_session_factory=MagicMock(return_value=realtime_session),
        control_plane=control_plane,
        alert_sink=AsyncMock(),
    )


async def _run(service):
    await service.handle_call(
        ticket="t1", inbound_audio=_frames(), send_outbound_audio=AsyncMock(),
        clear_outbound_audio=AsyncMock(), playback=PlaybackTracker(),
    )


@pytest.mark.asyncio
async def test_committed_turn_gets_the_persona_anchor_then_a_reply():
    calls = []
    realtime_session = AsyncMock()
    realtime_session.submit_message.side_effect = lambda role, text: calls.append((role, text))
    realtime_session.request_response.side_effect = lambda: calls.append("response.create")

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "item_user_1"})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _run(_service_with(realtime_session, _PERSONA_INSTRUCTIONS))

    assert calls[0] == ("system", "[The caller has just picked up the phone you called. Speak first.]")
    calls = calls[3:]  # pickup note, opening anchor, opening response.create
    assert len(calls) == 2
    role, anchor = calls[0]
    assert role == "system"
    assert "PERSONALITY ANCHOR" in anchor
    assert "- spoken_delivery" in anchor and "- humor_engine" in anchor
    assert calls[1] == "response.create"


@pytest.mark.asyncio
async def test_every_turn_is_anchored_and_anchors_are_not_deleted():
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        for i in range(3):
            yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": f"item_user_{i}"})
            yield RealtimeSessionEvent(type="response_created", payload={})
            yield RealtimeSessionEvent(type="response_done", payload={})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _run(_service_with(realtime_session, _PERSONA_INSTRUCTIONS))

    # pickup note + opening anchor + one anchor per turn; opening reply + one per turn
    assert realtime_session.submit_message.await_count == 5
    assert realtime_session.request_response.await_count == 4


@pytest.mark.asyncio
async def test_prompt_without_persona_sections_still_replies_without_an_anchor():
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})  # Lelik's opening line
        yield RealtimeSessionEvent(type="response_done", payload={})
        yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "item_user_1"})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _run(_service_with(realtime_session, "you are Lelik"))

    # Only the pickup note: no persona sections, so no anchor.
    assert [c.args[1] for c in realtime_session.submit_message.await_args_list] == [
        "[The caller has just picked up the phone you called. Speak first.]",
    ]
    assert realtime_session.request_response.await_count == 2


@pytest.mark.asyncio
async def test_turn_committed_while_a_response_is_active_starts_nothing():
    """response.create while a response is active is a call-ending provider error."""
    realtime_session = AsyncMock()

    async def events():
        yield RealtimeSessionEvent(type="response_created", payload={})
        yield RealtimeSessionEvent(type="turn_committed", payload={"item_id": "item_user_1"})

    realtime_session.receive_events = MagicMock(return_value=events())
    await _run(_service_with(realtime_session, _PERSONA_INSTRUCTIONS))

    # Only the opening happened: the committed turn started nothing while it was active.
    assert realtime_session.submit_message.await_count == 2  # pickup note + opening anchor
    realtime_session.request_response.assert_awaited_once()



@pytest.mark.asyncio
async def test_lelik_speaks_first_as_soon_as_the_session_opens():
    """He placed the call: the caller's own 'hello?' usually falls before the media stream
    exists, and waiting for another one left both sides silent on every live call."""
    calls = []
    realtime_session = AsyncMock()
    realtime_session.open.side_effect = lambda **kw: calls.append("open")
    realtime_session.submit_message.side_effect = lambda role, text: calls.append(("msg", text[:20]))
    realtime_session.request_response.side_effect = lambda: calls.append("response.create")

    async def events():
        return
        yield  # pragma: no cover

    realtime_session.receive_events = MagicMock(return_value=events())
    await _run(_service_with(realtime_session, _PERSONA_INSTRUCTIONS))

    assert calls[0] == "open"
    assert calls[1] == ("msg", "[The caller has just")
    assert calls[2] == ("msg", "PERSONALITY ANCHOR —")
    assert calls[3] == "response.create"
