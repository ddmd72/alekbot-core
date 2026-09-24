"""
Voice relay entrypoint — a SECOND Cloud Run **service** (RFC §4.13/§9.6),
separate from the main `alek-bot` app (`main.py`). Runs a plain WebSocket
server that speaks Twilio's Media Streams protocol and drives
`VoiceSessionService.handle_call` (Task 12) for each call.

Environment variables (static — set at relay deploy time, Task 14):
  OPENAI_API_KEY          Secret: OpenAI API key (realtime provider).
  CLOUD_RUN_SERVICE_URL   Base URL of the MAIN service, as seen from the
                           relay — used to call back into
                           /voice/session-config and /voice/submit-transcript.
                           Same key name and same "the main service's own
                           public URL" meaning `job_main.py` already uses for
                           its Cloud Tasks callback (`_build_task_queue()`);
                           this is a second, independent deploy unit with its
                           own environment, so the name is reused rather than
                           inventing a synonym for the identical purpose.
  BILLING_SLACK_WEBHOOK_URL
                           Ops Slack webhook. Required here (not optional, in
                           contrast to `main.py`'s AgentCoordinator wiring)
                           because `VoiceSessionService` calls
                           `self._alert_sink.post(...)` unconditionally on a
                           provider error event with no None-guard — passing
                           None would crash the call instead of alerting.
  PORT                    Cloud Run injects this; defaults to 8080 locally.

This is wiring, like `job_main.py` — no dedicated unit test file (confirmed
precedent: `job_main.py` has none either). Verified by manual read-through +
architecture test run instead (see task-13-report.md).
"""
import asyncio
import os
import signal
from pathlib import Path

import websockets

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.handlers.media_stream_handler import MediaStreamHandler
from src.services.voice_session_service import VoiceSessionService
from src.utils.logger import logger

# Breath-pulse filler played while Lelik thinks or waits (owner pick B2, 2026-09-24).
_THINKING_CUE_PATH = Path(__file__).parent / "src" / "assets" / "voice" / "thinking_cue.ulaw"

# Spoken as the owner's own first turn of every call (owner's wording, 2026-09-25): the same
# delivery rules as system text left Lelik sounding like a narrator; asked in the call, he
# changed at once, and his later turns follow his own first ones.
_CALLER_OPENING = (
    "Говори быстрее и человечнее, не как диктор. Не говори с расстановкой: внутри фразы — "
    "без пауз между словами. Не повторяй одну и ту же интонацию в каждой фразе — пусть она "
    "меняется, как в обычном разговоре. Поздоровайся со мной по имени и спроси, чем можешь "
    "помочь (отвечай, применяя юмор и голос)."
)


def _load_thinking_cue() -> bytes:
    return _THINKING_CUE_PATH.read_bytes()


def _fetch_id_token(audience: str) -> str:
    """Mint a Google-signed OIDC identity token for the relay's own Cloud Run
    service identity (ADC — the metadata server on Cloud Run), for calling
    back into the main service's OIDC-protected /voice/* routes. Audience
    value itself is not checked by the verifier (see
    `src/web/worker_oidc_verifier.py` docstring — identity-only check), but
    `fetch_id_token` requires a well-formed audience argument regardless."""
    import google.auth.transport.requests
    import google.oauth2.id_token

    request = google.auth.transport.requests.Request()
    return google.oauth2.id_token.fetch_id_token(request, audience)


async def main() -> None:
    main_service_url = os.environ["CLOUD_RUN_SERVICE_URL"]
    openai_api_key = os.environ["OPENAI_API_KEY"]
    billing_webhook_url = os.environ["BILLING_SLACK_WEBHOOK_URL"]

    control_plane = HttpCallControlPlaneAdapter(
        main_service_url=main_service_url,
        id_token_provider=lambda: _fetch_id_token(main_service_url),
    )
    alert_sink = SlackWebhookAdapter(webhook_url=billing_webhook_url)
    session_service = VoiceSessionService(
        realtime_session_factory=lambda: OpenAIRealtimeAdapter(api_key=openai_api_key),
        control_plane=control_plane,
        alert_sink=alert_sink,
        # Owner's call 2026-09-23, raised from spike 0.4's `medium`: the per-turn anchor asks
        # the model to plan each sentence's rhythm and emotion before speaking, and that
        # planning happens in reasoning. Cost: reasoning bills as text output ($24/1M).
        reasoning_effort="high",
        # Owner's call 2026-09-25: line noise and "uh-huh"s cut replies at ~400 ms; interrupting
        # Lelik now takes a second of speech.
        barge_in_min_speech_s=1.0,
        # After the one "still there?", this much more silence hangs up (voicemail, a phone put down).
        hangup_after_silence_s=20.0,
        caller_opening=_CALLER_OPENING,
        # thinking_cue deliberately not passed (off): with it on, Lelik's replies were cut
        # after ~0.4-0.8 s on the live calls of 2026-09-24 and stopped when the relay was
        # routed back to a revision without it. Cause not found yet; the clip stays shipped.
    )
    handler = MediaStreamHandler(session_service=session_service)

    async def process_request(connection, request):
        # Mirrors scripts/voice/test_mulaw_relay_poc.py's process_request: a real
        # WebSocket upgrade (the Media Stream itself) falls through to the normal
        # handshake (None); a plain HTTP request (e.g. Cloud Run health probe) gets
        # a trivial 200 so the revision is marked healthy without needing a TwiML
        # response here — Twilio's own webhook (returning TwiML) is served by the
        # MAIN service's /voice/answer route (Task 8/9), not by this relay.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        return websockets.Response(200, "OK", websockets.Headers(), b"ok")

    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    port = int(os.environ.get("PORT", "8080"))
    async with websockets.serve(handler.handle_connection, "0.0.0.0", port, process_request=process_request):
        logger.info(f"voice relay listening on :{port}")
        await shutdown_event.wait()
        logger.info("voice relay shutting down")


if __name__ == "__main__":
    asyncio.run(main())
