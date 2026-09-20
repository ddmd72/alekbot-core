#!/usr/bin/env python3
"""
POC: two-way μ-law audio relay between a Twilio Media Stream and a realtime
provider (OpenAI or xAI, selected by PROVIDER env var). No resampling —
both sides speak audio/x-mulaw 8kHz. Run, then point a Twilio number's
Voice webhook at this process's /twiml endpoint via an ngrok tunnel.
"""
import asyncio
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from src.config.settings import load_settings

import websockets

PROVIDER = os.getenv("PROVIDER", "openai")  # "openai" | "xai"


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
    config = load_settings()
    provider_url, headers = provider_ws_url_and_headers(config)
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


async def run_poc():
    print(f"Relay listening on ws://0.0.0.0:8765 — provider={PROVIDER}")
    print("Expose with: ngrok http 8765 --scheme=http (Twilio needs wss:// via ngrok's https URL)")
    # NOTE: the brief's original `from websockets.server import serve as ws_serve` resolves to
    # `websockets.legacy.server.serve` on the pinned websockets==15.0.1 — that module is
    # deprecated and emits a DeprecationWarning per call (confirmed via
    # inspect.signature(websockets.server.serve) + module attribute before writing this).
    # `websockets.serve` (top-level) is the current asyncio-based implementation; its handler
    # signature (single ServerConnection arg, no path) matches handle_twilio_stream() as written,
    # so no other change was needed.
    async with websockets.serve(handle_twilio_stream, "0.0.0.0", 8765):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(run_poc())
