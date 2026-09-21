import asyncio
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable, Optional

from src.domain.request_context import RequestContext
from src.domain.voice_audio_frame import AudioFrame
from src.domain.voice_call_buffer import VoiceCallBuffer, VoiceTurnSegment
from src.ports.alert_sink import AlertSinkPort
from src.ports.call_control_plane_port import CallControlPlanePort
from src.ports.realtime_session_port import RealtimeSessionPort
from src.utils.logger import logger

_UNKNOWN_MODEL_LABEL = "unknown"


class VoiceSessionService:
    """The relay's session loop (RFC §4.5). Not an agent - REQ-ARCH-03/-30
    forbid naming this *Agent (RFC §4.4)."""

    def __init__(
        self,
        realtime_session_factory: Callable[[], RealtimeSessionPort],
        control_plane: CallControlPlanePort,
        alert_sink: AlertSinkPort,
        reasoning_effort: str = "medium",
    ) -> None:
        self._session_factory = realtime_session_factory
        self._control_plane = control_plane
        self._alert_sink = alert_sink
        self._reasoning_effort = reasoning_effort

    async def handle_call(
        self,
        ticket: str,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
    ) -> None:
        config = await self._control_plane.fetch_session_config(ticket)
        async with RequestContext(user_id=config["user_id"], account_id=config["account_id"]):
            await self._run_call(ticket, config, inbound_audio, send_outbound_audio)

    async def _run_call(
        self,
        ticket: str,
        config: dict,
        inbound_audio: AsyncIterator[AudioFrame],
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
    ) -> None:
        session = self._session_factory()
        buffer = VoiceCallBuffer(call_id=ticket)
        forward_task: Optional[asyncio.Task] = None
        consume_task: Optional[asyncio.Task] = None

        try:
            # open() is inside the try/finally, not before it: a failed open (e.g. a
            # transient connection error to the provider) must still reach close()
            # and submit_transcript() below - otherwise the relay's one-call-per-user
            # marker (RFC §3) only releases via TTL instead of immediately, and the
            # failure leaves no record on the main-service side.
            try:
                await session.open(instructions=config["instructions"], reasoning_effort=self._reasoning_effort, tools=[])
            except Exception as exc:
                logger.error(f"voice call {ticket}: failed to open realtime session: {exc}")
                raise

            # Inbound forwarding and outbound event consumption are two independent
            # streams (inbound audio keeps arriving from the caller regardless of
            # provider turn boundaries) - they must run as real concurrent tasks,
            # not a fire-and-forget task raced against a synchronous loop. A bare
            # `asyncio.ensure_future(...)` followed by an immediate `.cancel()` on
            # loop exit never gives the forwarding task a chance to actually run
            # if nothing in the receive loop truly suspends the event loop first -
            # asyncio.wait() below is what forces that handoff.
            forward_task = asyncio.ensure_future(self._forward_inbound(session, inbound_audio))
            consume_task = asyncio.ensure_future(self._consume_events(ticket, session, buffer, send_outbound_audio))
            await asyncio.wait({forward_task, consume_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            tasks = [task for task in (forward_task, consume_task) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    # Logged, not re-raised: this is a top-level call loop with no
                    # caller to propagate to - the call simply ends here, which is
                    # the correct outcome for a crashed forward/consume loop. The
                    # "error" event path above already alerts on provider-side
                    # failures; this only catches an unexpected bug in the loop
                    # itself, and still needs to be visible in logs.
                    if isinstance(result, Exception):
                        logger.error(f"voice call {ticket}: session loop crashed: {result}")
            await session.close()
            await self._control_plane.submit_transcript(
                call_id=ticket,
                user_id=config["user_id"],
                account_id=config["account_id"],
                buffer=buffer,
            )

    async def _consume_events(
        self,
        ticket: str,
        session: RealtimeSessionPort,
        buffer: VoiceCallBuffer,
        send_outbound_audio: Callable[[AudioFrame], Awaitable[None]],
    ) -> None:
        turn_start: Optional[datetime] = None
        pending_request_text = ""
        pending_response_text = ""

        async for event in session.receive_events():
            if event.type == "response_created":
                turn_start = datetime.now(timezone.utc)
            elif event.type == "audio_delta":
                await send_outbound_audio(event.payload["frame"])
            elif event.type == "user_transcript":
                pending_request_text += event.payload["text"]
            elif event.type == "model_transcript":
                pending_response_text += event.payload["text"]
            elif event.type == "response_done":
                usage = event.payload.get("usage", {})
                if usage:
                    # The provider labels its own usage - VoiceSessionService is
                    # provider-agnostic (RealtimeSessionPort), so it must not
                    # hardcode a specific model string (REQ-ARCH-01 layer rule:
                    # provider-specific model ids live in adapters/, not services/).
                    buffer.add_usage(event.payload.get("model", _UNKNOWN_MODEL_LABEL), **usage)
                buffer.add_turn(VoiceTurnSegment(
                    request_text=pending_request_text,
                    response_text=pending_response_text,
                    started_at=turn_start or datetime.now(timezone.utc),
                    ended_at=datetime.now(timezone.utc),
                ))
                pending_request_text = ""
                pending_response_text = ""
            elif event.type == "error":
                logger.error(f"voice call {ticket}: provider error {event.payload.get('message')}")
                await self._alert_sink.post(f"Voice call {ticket} provider error: {event.payload.get('message')}")
                return

    async def _forward_inbound(self, session: RealtimeSessionPort, inbound_audio: AsyncIterator[AudioFrame]) -> None:
        async for frame in inbound_audio:
            await session.send_audio(frame)
