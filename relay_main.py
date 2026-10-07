"""
Voice relay entrypoint — a SECOND Cloud Run **service** (RFC §4.13/§9.6),
separate from the main `alek-bot` app (`main.py`). Runs a plain WebSocket
server that speaks Twilio's Media Streams protocol and drives
`VoiceSessionService.handle_call` (Task 12) for each call.

Environment variables (static — set at relay deploy time, Task 14):
  OPENAI_API_KEY          Secret: OpenAI API key (realtime provider).
  XAI_API_KEY             Secret: xAI API key (realtime provider). Which provider a call
                           runs on, and with which voice, effort and turn ownership, comes
                           per call from the session config (VOICE_MULTI_PROVIDER_RFC §4.2).
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
  VOICE_WEB_REASONING_EFFORT
                           Optional knob (unset = each call's own spec) for the web-call
                           VoiceSessionService only (VOICE_WEB_TRANSPORT_RFC
                           §5.4) — read with os.environ.get, not load_settings
                           (CLAUDE.md: optional knobs, not secrets/required
                           config, are os.getenv'd at their call site).
  VOICE_WEB_CALLER_OPENING
                           Optional knob (default "on"); "off" disables the
                           owner's-first-turn opening on web calls only, same
                           text as the phone path (_CALLER_OPENING).
  PORT                    Cloud Run injects this; defaults to 8080 locally.

This is wiring, like `job_main.py` — no dedicated unit test file (confirmed
precedent: `job_main.py` has none either). Verified by manual read-through +
architecture test run instead (see task-13-report.md).
"""
import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Callable, Dict, Type

import websockets

from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.adapters.openai_realtime_adapter import OpenAIRealtimeAdapter
from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.adapters.slack.webhook_adapter import SlackWebhookAdapter
from src.domain.voice_audio_format import MULAW_8K, PCM16_24K, AudioFormat
from src.domain.voice_session_spec import VoiceSessionSpec
from src.handlers.media_stream_handler import MediaStreamHandler
from src.handlers.sfu_stream_handler import SfuStreamHandler
from src.ports.realtime_session_port import RealtimeSessionPort
from src.services.voice_session_service import VoiceSessionService
from src.utils.logger import logger

# The `websockets` library logs any TCP connection that never completes a WS handshake
# (port scanners, bare health pings) as an ERROR on "websockets.server" — this is documented
# upstream behavior, not a call-handling fault (prod log audit C-19: a third of this service's
# weekly ERRORs). Quieted here, not in src/utils/logger.py, since only this raw-websockets
# entrypoint triggers it.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

# Breath-pulse filler played while Lelik thinks or waits (owner pick B2, 2026-09-24).
_THINKING_CUE_PATH = Path(__file__).parent / "src" / "assets" / "voice" / "thinking_cue.ulaw"

# The owner's first turn of every call: only a cue for Lelik to speak first (owner's wording,
# 2026-09-28). Delivery rules no longer ride here — one place for them, not three. "One short
# sentence" because the greeting's first seconds cannot be interrupted (greeting_guard_s). The language
# reminder matters: this English item is the first input, and LANG_MIRROR would otherwise mirror it.
_CALLER_OPENING = "Welcome the user in one short sentence. Do not forget to follow language settings"


def _load_thinking_cue() -> bytes:
    return _THINKING_CUE_PATH.read_bytes()


# Provider name (VoiceSessionSpec.provider) -> adapter; the keys of VOICE_PROVIDER_PROFILES.
_ADAPTERS: Dict[str, Type[RealtimeSessionPort]] = {
    "openai": OpenAIRealtimeAdapter,
    "xai": XaiRealtimeAdapter,
}


def _realtime_session_factory(api_keys: Dict[str, str],
                              audio_format: AudioFormat) -> Callable[[VoiceSessionSpec], RealtimeSessionPort]:
    """One adapter per call, for the provider the call's spec names (VOICE_MULTI_PROVIDER_RFC §4.2)."""
    def build(spec: VoiceSessionSpec) -> RealtimeSessionPort:
        adapter_cls = _ADAPTERS[spec.provider]
        if spec.turn_ownership not in adapter_cls.supported_turn_ownership:
            raise ValueError(f"{adapter_cls.__name__} does not implement {spec.turn_ownership.value} turn ownership")
        return adapter_cls(api_key=api_keys[spec.provider], audio_format=audio_format, voice=spec.voice)
    return build


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
    api_keys = {
        "openai": os.environ["OPENAI_API_KEY"],
        # The stored secret ends in "\n" (an illegal header value); see decisions/grok_revival_2026_08.md.
        "xai": os.environ["XAI_API_KEY"].strip(),
    }
    billing_webhook_url = os.environ["BILLING_SLACK_WEBHOOK_URL"]

    control_plane = HttpCallControlPlaneAdapter(
        main_service_url=main_service_url,
        id_token_provider=lambda: _fetch_id_token(main_service_url),
    )
    alert_sink = SlackWebhookAdapter(webhook_url=billing_webhook_url)
    session_service = VoiceSessionService(
        realtime_session_factory=_realtime_session_factory(api_keys, MULAW_8K),
        control_plane=control_plane,
        alert_sink=alert_sink,
        # Owner's call 2026-09-25: line noise and "uh-huh"s cut replies at ~400 ms; interrupting
        # Lelik now takes a second of speech.
        barge_in_min_speech_s=1.0,
        # After the one "still there?", this much more silence hangs up (voicemail, a phone put down).
        hangup_after_silence_s=20.0,
        caller_opening=_CALLER_OPENING,
        # Owner's call 2026-09-28: while a delegation is out, each "still waiting" note comes after
        # a random 8-15 s of quiet (silence_timeout_s is the floor), not on a fixed beat.
        waiting_gap_max_s=15.0,
        # Owner's call 2026-09-28: speech in the greeting's first 3 s never interrupts it — the
        # browser's echo canceller is still adapting and Lelik's own voice came back as the caller.
        greeting_guard_s=3.0,
        # Owner's call 2026-09-28: an ask_alek took 105 s live and was lost at 90 s. What still
        # outlasts this is posted to the user's chat by the main service.
        delegation_timeout_s=300.0,
        # thinking_cue deliberately not passed (off): with it on, Lelik's replies were cut
        # after ~0.4-0.8 s on the live calls of 2026-09-24 and stopped when the relay was
        # routed back to a revision without it. Cause not found yet; the clip stays shipped.
    )
    handler = MediaStreamHandler(session_service=session_service)

    # Web calls (VOICE_WEB_TRANSPORT_RFC §5.4): a second VoiceSessionService/OpenAIRealtimeAdapter
    # pair at 24 kHz PCM (the phone pair above stays 8 kHz mu-law), routed by path alongside the
    # Twilio Media Stream handler. Defaults mirror the phone service so the only variable UAT sees
    # first is the transport itself; the two env knobs let UAT tune reasoning cost/latency and the
    # caller-opening line without a redeploy.
    # Unset = the call's own spec. Set, it overrides the effort of every web call, whichever
    # provider it runs on, and must be a value that provider accepts.
    web_reasoning = os.environ.get("VOICE_WEB_REASONING_EFFORT") or None
    web_opening = _CALLER_OPENING if os.environ.get("VOICE_WEB_CALLER_OPENING", "on").strip().lower() == "on" else None
    web_session_service = VoiceSessionService(
        realtime_session_factory=_realtime_session_factory(api_keys, PCM16_24K),
        control_plane=control_plane,
        alert_sink=alert_sink,
        reasoning_effort=web_reasoning,
        barge_in_min_speech_s=1.0,
        hangup_after_silence_s=20.0,
        caller_opening=web_opening,
        waiting_gap_max_s=15.0,
        greeting_guard_s=3.0,
        # Owner's call 2026-09-28: same 300 s as the phone path.
        delegation_timeout_s=300.0,
    )
    sfu_handler = SfuStreamHandler(session_service=web_session_service)

    async def route(connection):
        path = connection.request.path
        if path.startswith("/sfu/"):
            await sfu_handler.handle_connection(connection, path)
        else:
            await handler.handle_connection(connection)

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
    async with websockets.serve(route, "0.0.0.0", port, process_request=process_request):
        logger.info(f"voice relay listening on :{port}")
        await shutdown_event.wait()
        logger.info("voice relay shutting down")


if __name__ == "__main__":
    asyncio.run(main())
