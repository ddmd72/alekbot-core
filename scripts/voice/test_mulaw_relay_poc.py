#!/usr/bin/env python3
"""
POC: two-way μ-law audio relay between a Twilio Media Stream and a realtime
provider (OpenAI or xAI, selected by PROVIDER env var). No resampling —
both sides speak audio/x-mulaw 8kHz. Run, expose via ngrok, then point a
Twilio number's Voice webhook directly at the ngrok URL (any path, plain
HTTP webhook — not a TwiML Bin). Live discovery 2026-09-20: TwiML Bins are
US1-only and return 401 for numbers routed via IE1/AU1, so this process
serves its own static TwiML for plain HTTP requests (Twilio's webhook
fetch) and hands real WebSocket upgrades (the Media Stream itself) to the
relay handler — one port, one ngrok tunnel, no Bin involved.
"""
import asyncio
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from src.config.settings import load_settings

import websockets

PROVIDER = os.getenv("PROVIDER", "openai")  # "openai" | "xai"
CONFIG = None  # set once in run_poc() before the server starts — see note there


def provider_ws_url_and_headers(config: dict) -> tuple[str, dict]:
    # both connection shapes verified live 2026-09-20 — see plan's "Verified against live docs"
    if PROVIDER == "openai":
        return (
            "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1",
            {"Authorization": f"Bearer {config['OPENAI_API_KEY']}"},
        )
    return (
        "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0",
        {"Authorization": f"Bearer {config['XAI_API_KEY']}"},
    )


def session_update_event() -> dict:
    # identical schema for both providers — xAI's realtime endpoint is OpenAI-Realtime-API-
    # compatible (confirmed live 2026-09-20). This is the field to watch in session.updated
    # for a silent reversion away from audio/pcmu.
    return {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "audio": {
                "input": {"format": {"type": "audio/pcmu"}},
                "output": {"format": {"type": "audio/pcmu"}},
            },
        },
    }


async def handle_twilio_stream(twilio_ws):
    provider_url, headers = provider_ws_url_and_headers(CONFIG)
    # NOTE: installed websockets==15.0.1 renamed connect()'s `extra_headers` kwarg to
    # `additional_headers` (same rename Task 1's POC already hit — confirmed via
    # inspect.signature(websockets.connect) before writing this).
    async with websockets.connect(provider_url, additional_headers=headers) as provider_ws:
        await provider_ws.send(json.dumps(session_update_event()))
        stream_sid = None

        async def twilio_to_provider():
            nonlocal stream_sid
            async for raw in twilio_ws:
                msg = json.loads(raw)
                if msg["event"] == "start":
                    stream_sid = msg["start"]["streamSid"]
                    print(f"[{PROVIDER}] stream started: {stream_sid}")
                elif msg["event"] == "media":
                    await provider_ws.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": msg["media"]["payload"],  # already base64 mulaw — no decode/re-encode
                    }))
                elif msg["event"] == "stop":
                    print(f"[{PROVIDER}] stream stopped")
                    break

        async def provider_to_twilio():
            async for raw in provider_ws:
                event = json.loads(raw)
                # "response.output_audio.delta" is the current GA name (was "response.audio.delta"
                # pre-GA); a known community-reported bug means it sometimes never arrives — see
                # this plan's "Verified against live docs" note if no audio comes through at all.
                if event["type"] in ("response.output_audio.delta", "response.audio.delta"):
                    payload = event.get("delta") or event.get("audio")
                    if payload and stream_sid:
                        await twilio_ws.send(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }))
                elif event["type"] == "session.updated":
                    # log the ECHOED format back — this is the pcm16-reversion check
                    print(f"[{PROVIDER}] session.updated audio config: {event['session'].get('audio')}")

        await asyncio.gather(twilio_to_provider(), provider_to_twilio())


def process_request(connection, request):
    """Serve static TwiML for a plain HTTP request (Twilio's Voice webhook fetch);
    return None for a real WebSocket upgrade (the Media Stream itself) so the
    normal handshake proceeds into handle_twilio_stream(). This replaces the
    TwiML-Bin approach, which 401s for numbers routed via IE1/AU1 (US1-only)."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    host = request.headers.get("Host", "localhost:8765")
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{host}/" /></Connect></Response>'
    )
    body = twiml.encode("utf-8")
    headers = websockets.Headers()
    headers["Content-Type"] = "text/xml"
    headers["Content-Length"] = str(len(body))
    return websockets.Response(200, "OK", headers, body)


async def run_poc():
    global CONFIG
    # Load once, up front, and fail fast — before the "listening" banner prints. Loading inside
    # handle_twilio_stream() per-connection would let the banner look like a green light while a
    # missing/bad OPENAI_API_KEY/XAI_API_KEY only surfaces after the owner has started ngrok,
    # built the TwiML Bin, pointed the number at it, and dialed in.
    try:
        CONFIG = load_settings()
    except Exception as exc:
        print(f"FATAL: load_settings() failed before startup — fix this before wiring up ngrok/Twilio: {exc}")
        sys.exit(1)

    print(f"Relay listening on ws://0.0.0.0:8765 — provider={PROVIDER}")
    print("Expose with: ngrok http 8765")
    print("Point the Twilio number's Voice webhook directly at the ngrok https:// URL")
    print("(any path, HTTP POST) — no TwiML Bin needed, this process serves its own TwiML.")
    # NOTE: the brief's original `from websockets.server import serve as ws_serve` resolves to
    # `websockets.legacy.server.serve` on the pinned websockets==15.0.1 — that module is
    # deprecated and emits a DeprecationWarning per call (confirmed via
    # inspect.signature(websockets.server.serve) + module attribute before writing this).
    # `websockets.serve` (top-level) is the current asyncio-based implementation; its handler
    # signature (single ServerConnection arg, no path) matches handle_twilio_stream() as written,
    # so no other change was needed.
    async with websockets.serve(handle_twilio_stream, "0.0.0.0", 8765, process_request=process_request):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(run_poc())
