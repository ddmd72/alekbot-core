#!/usr/bin/env python3
"""
POC: late function_call_output injection on OpenAI and xAI Realtime APIs.
Opens a text-only realtime session, triggers a tool call, waits 20-30s
(simulating a slow ask_alek round trip) before submitting the tool result,
and checks whether the model incorporates it coherently.
"""
import asyncio
import json
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from src.config.settings import load_settings

import websockets

TOOL_DEF = {
    "type": "function",
    "name": "lookup_fact",
    "description": "Look up a fact about the user. Always call this when asked a personal question.",
    "parameters": {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
    },
}

DELAYED_ANSWER = "The user's favorite color is teal."


def _session_config(provider: str) -> dict:
    """Per-provider session.update body.

    OpenAI GA requires "type": "realtime" + "output_modalities" (confirmed live 2026-09-20:
    the API rejected the brief's original "modalities" key with `invalid_request_error:
    missing_required_parameter session.type`; corrected shape cross-checked against
    developers.openai.com/api/reference/resources/realtime/client-events). Without this the
    session silently defaults to audio output and the model never emits a function_call at
    all — verified: it hallucinates an answer instead.

    xAI does NOT accept the GA shape — it uses the older "modalities" key (no "type" field).
    It accepts and echoes back "modalities": ["text"] in session.updated, but empirically
    (confirmed live, repeated) grok-voice-think-fast-2.0 keeps emitting audio output
    regardless — no response.output_text.delta is ever produced. This is a genuine provider
    limitation, not a script bug; see the audio-transcript fallback in run_provider_case.
    """
    if provider == "openai":
        return {"type": "realtime", "output_modalities": ["text"], "tools": [TOOL_DEF], "tool_choice": "auto"}
    return {"modalities": ["text"], "tools": [TOOL_DEF], "tool_choice": "auto"}


async def run_provider_case(provider: str, ws_url: str, headers: dict, delay_s: int, interrupt: bool):
    """Open a session, trigger a tool call, submit the result late. Returns (ok: bool, transcript: list[str])."""
    transcript = []
    # NOTE: installed websockets==15.0.1 renamed connect()'s `extra_headers` kwarg to
    # `additional_headers` (confirmed via inspect.signature before running) — using
    # `additional_headers` here instead of the brief's `extra_headers`.
    async with websockets.connect(ws_url, additional_headers=headers) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": _session_config(provider),
        }))
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "What's my favorite color? Use the lookup_fact tool."}
            ]},
        }))
        await ws.send(json.dumps({"type": "response.create"}))

        call_id = None
        response_active = True
        async for raw in ws:
            event = json.loads(raw)
            transcript.append(f"{provider} recv: {event.get('type')}")
            if event["type"] == "response.function_call_arguments.done":
                call_id = event["call_id"]
            if event["type"] == "response.done":
                response_active = False
                if call_id:
                    break  # tool was requested, response finished — now the late-injection window starts

        if not call_id:
            return False, transcript + ["FAIL: no tool call was requested"]

        print(f"[{provider}] waiting {delay_s}s before submitting function_call_output "
              f"(interrupt={interrupt}) ...")
        if interrupt:
            # simulate "the user barges in" mid-wait: send another user turn before the tool result
            await asyncio.sleep(delay_s / 2)
            await ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "Actually, never mind the color — how are you?"}
                ]},
            }))
            await ws.send(json.dumps({"type": "response.create"}))
            # drain this interim response before continuing the delay
            async for raw in ws:
                event = json.loads(raw)
                transcript.append(f"{provider} recv (interim): {event.get('type')}")
                if event["type"] == "response.done":
                    break
            await asyncio.sleep(delay_s / 2)
        else:
            await asyncio.sleep(delay_s)

        t0 = time.monotonic()
        await ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": DELAYED_ANSWER},
        }))
        await ws.send(json.dumps({"type": "response.create"}))

        final_text = ""
        audio_transcript = ""
        async for raw in ws:
            event = json.loads(raw)
            transcript.append(f"{provider} recv (late): {event.get('type')}")
            # "response.output_text.delta" is the GA name (was "response.text.delta" pre-GA) —
            # same rename pattern as response.output_audio.delta (see plan's verified-docs note)
            if event["type"] in ("response.output_text.delta", "response.text.delta"):
                final_text += event.get("delta", "")
            # Fallback channel: xAI's grok-voice-think-fast-2.0 never honors text-only output
            # (confirmed live — see _session_config docstring) and only ever answers with
            # audio + a spoken transcript. response.output_audio_transcript.done carries the
            # full utterance on both providers, so read it as a second evidence source rather
            # than let a provider's forced-audio behavior masquerade as an incoherence FAIL.
            if event["type"] == "response.output_audio_transcript.done":
                audio_transcript += event.get("transcript", "")
            if event["type"] == "response.done":
                break
            if event["type"] == "error":
                return False, transcript + [f"FAIL: error event {event}"]

        elapsed = time.monotonic() - t0
        combined_text = f"{final_text} {audio_transcript}"
        coherent = "teal" in combined_text.lower()
        transcript.append(
            f"{provider} final_text={final_text!r} audio_transcript={audio_transcript!r} "
            f"elapsed={elapsed:.2f}s coherent={coherent}"
        )
        return coherent, transcript


async def run_poc():
    config = load_settings()
    results = {}

    # --- OpenAI case --- (GA Realtime API, verified live 2026-09-20 — no OpenAI-Beta header,
    # that was the beta API retired 2026-05-12)
    # Full 2x2 factorial (delay x interrupt) per provider, per the plan's "8 cases total"
    # (2 delays x 2 interrupt settings x 2 providers) — the brief's pasted loop only zipped
    # the diagonal [(20, False), (30, True)], which is 4 cases, not 8; expanded here to match
    # the repeated "8 cases" spec in the brief's own Step 3 / self-review checklist.
    openai_ws_url = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"
    openai_headers = {"Authorization": f"Bearer {config['OPENAI_API_KEY']}"}
    for delay in (20, 30):
        for interrupt in (False, True):
            ok, log = await run_provider_case("openai", openai_ws_url, openai_headers, delay, interrupt)
            results[f"openai_delay{delay}_interrupt{interrupt}"] = ok
            print("\n".join(log))

    # --- xAI case --- (OpenAI-Realtime-API-compatible per docs.x.ai, verified live 2026-09-20)
    xai_ws_url = "wss://api.x.ai/v1/realtime?model=grok-voice-think-fast-2.0"
    xai_headers = {"Authorization": f"Bearer {config['XAI_API_KEY']}"}
    for delay in (20, 30):
        for interrupt in (False, True):
            ok, log = await run_provider_case("xai", xai_ws_url, xai_headers, delay, interrupt)
            results[f"xai_delay{delay}_interrupt{interrupt}"] = ok
            print("\n".join(log))

    print("\n=== SUMMARY ===")
    for k, v in results.items():
        print(f"{k}: {'PASS' if v else 'FAIL'}")


if __name__ == "__main__":
    asyncio.run(run_poc())
