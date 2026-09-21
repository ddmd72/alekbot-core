import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.voice_session_service import VoiceSessionService
from src.domain.voice_audio_frame import AudioFrame
from src.ports.realtime_session_port import RealtimeSessionEvent


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
        ticket="t1", inbound_audio=inbound, send_outbound_audio=AsyncMock(side_effect=outbound_sent.append)
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

    await service.handle_call(ticket="t1", inbound_audio=_frames(), send_outbound_audio=AsyncMock())

    alert_sink.post.assert_awaited_once()
    assert "socket dropped" in alert_sink.post.await_args.args[0]
    control_plane.submit_transcript.assert_awaited_once()  # a dropped call still summarizes (RFC §4.9)
    submit_kwargs = control_plane.submit_transcript.await_args.kwargs
    assert submit_kwargs["call_id"] == "t1"
    assert submit_kwargs["user_id"] == "u1"
    assert submit_kwargs["account_id"] == "a1"
