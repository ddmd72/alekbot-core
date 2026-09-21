#!/usr/bin/env python3
"""
POC: reasoning effort vs. retention/latency/cost tradeoff on OpenAI's Realtime API.

Opens a text-only realtime session per `reasoning_effort` level (minimal/low/medium/high/xhigh),
runs a fixed 6-turn retention probe (plant a fact, 4 filler turns, then ask for it back), and
reports whether the final turn recalls the fact, per-turn latency, and token usage/cost.

OpenAI only — see RFC Phase 0.4 / task-5-brief.md Step 1: xAI's `grok-voice-think-fast-2.0`
reasoning-effort support was never checked against docs.x.ai and is out of scope here; the
decision record notes this explicitly rather than assuming parity.

Standalone script, house pattern from scripts/voice/test_late_function_call_output_poc.py:
plain asyncio + websockets, load_settings() for the API key, no pytest.
"""
import asyncio
import json
import os
import statistics
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from src.config.settings import load_settings

import websockets

REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]

RETENTION_PROBE_SCRIPT = [
    "My colleague Ivan Petrov handles the Q3 budget — remember that.",
    "What's the weather like today?",
    "Tell me a fact about Valencia.",
    "What time is it in Tokyo right now?",
    "Can you count from one to five?",
    "Who handles the Q3 budget?",  # retention check — correct answer: Ivan Petrov
]

TEXT_INPUT_RATE_PER_M = 4.0
TEXT_OUTPUT_RATE_PER_M = 24.0

OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1"


def _session_config(effort: str) -> dict:
    """GA text-only session config with reasoning.effort set.

    "type": "realtime" + "output_modalities" is the confirmed GA shape (see
    test_late_function_call_output_poc.py's _session_config docstring — the older "modalities"
    key without "type" is rejected). "reasoning": {"effort": effort} is the field path confirmed
    against OpenAI's live client-events reference per the plan's "Verified against live docs"
    section — not re-verified in this script, per task-5-brief.md Step 1.
    """
    return {
        "type": "realtime",
        "output_modalities": ["text"],
        "reasoning": {"effort": effort},
    }


async def _run_turn(ws, text: str) -> dict:
    """Send one user turn, wait for response.done, return timing/usage/text for that turn.

    Catch-and-continue on an "error" event, matching test_late_function_call_output_poc.py's
    run_provider_case pattern (returns a failure signal instead of raising) — so one bad turn
    doesn't crash the whole multi-level run. Returns {"error": None, ...} on success or
    {"error": <event or reason>, "text": ..., "elapsed_s": ..., "usage": None} on failure.
    """
    await ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": text}
        ]},
    }))

    t0 = time.monotonic()
    await ws.send(json.dumps({"type": "response.create"}))

    final_text = ""
    async for raw in ws:
        event = json.loads(raw)
        if event["type"] in ("response.output_text.delta", "response.text.delta"):
            final_text += event.get("delta", "")
        if event["type"] == "response.done":
            elapsed = time.monotonic() - t0
            response = event.get("response", {})
            usage = response.get("usage")
            return {"text": final_text, "elapsed_s": elapsed, "usage": usage, "error": None}
        if event["type"] == "error":
            elapsed = time.monotonic() - t0
            return {"text": final_text, "elapsed_s": elapsed, "usage": None, "error": event}

    # connection closed without a terminal event
    elapsed = time.monotonic() - t0
    return {"text": final_text, "elapsed_s": elapsed, "usage": None,
            "error": {"type": "connection_closed_without_response.done"}}


def _failed_summary(effort: str, reason, turn_results: list) -> dict:
    """Build a FAIL summary for an effort level that couldn't complete all 6 turns —
    catch-and-continue: the caller still gets a row in the final table instead of a crash.
    """
    latencies = [r["elapsed_s"] for r in turn_results]
    print(f"  --- summary: effort={effort} FAILED ({len(turn_results)}/{len(RETENTION_PROBE_SCRIPT)} "
          f"turns completed) reason={reason}")
    return {
        "effort": effort,
        "retention_pass": False,
        "final_reply": None,
        "total_elapsed_s": sum(latencies) if latencies else 0.0,
        "p50_latency_s": statistics.median(latencies) if latencies else 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": None,
        "cost_usd": 0.0,
    }


async def run_effort_level(effort: str, config: dict) -> dict:
    """Run the full 6-turn retention probe at one reasoning-effort level.

    Catch-and-continue on any error (session.update rejection, a mid-probe "error" event, or a
    transport-level failure) — matching test_late_function_call_output_poc.py's pattern of
    returning a failure signal rather than raising, so one bad effort level doesn't take down
    the whole 5-level run.
    """
    headers = {"Authorization": f"Bearer {config['OPENAI_API_KEY']}"}
    turn_results = []

    print(f"\n=== reasoning_effort={effort} ===")
    try:
        async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": _session_config(effort),
            }))
            # drain session.updated before the first turn
            async for raw in ws:
                event = json.loads(raw)
                if event["type"] == "session.updated":
                    break
                if event["type"] == "error":
                    return _failed_summary(effort, f"session.update rejected: {event}", turn_results)

            for i, turn_text in enumerate(RETENTION_PROBE_SCRIPT):
                result = await _run_turn(ws, turn_text)
                if result["error"] is not None:
                    print(f"  turn {i+1}/6 FAIL: error event {result['error']}")
                    return _failed_summary(effort, f"turn {i+1} error: {result['error']}", turn_results)
                turn_results.append(result)
                print(f"  turn {i+1}/6 [{result['elapsed_s']:.2f}s]: {turn_text!r} -> {result['text']!r}")
    except Exception as exc:
        return _failed_summary(effort, f"transport exception: {exc}", turn_results)

    final_reply = turn_results[-1]["text"]
    retention_pass = "ivan petrov" in final_reply.lower()

    latencies = [r["elapsed_s"] for r in turn_results]
    total_elapsed = sum(latencies)
    p50_latency = statistics.median(latencies)

    total_input_tokens = 0
    total_output_tokens = 0
    total_reasoning_tokens = 0
    reasoning_split_available = False
    for r in turn_results:
        usage = r["usage"] or {}
        total_input_tokens += usage.get("input_tokens", 0) or 0
        total_output_tokens += usage.get("output_tokens", 0) or 0
        output_details = usage.get("output_token_details") or {}
        if "reasoning_tokens" in output_details:
            reasoning_split_available = True
            total_reasoning_tokens += output_details.get("reasoning_tokens", 0) or 0

    cost = (total_input_tokens * TEXT_INPUT_RATE_PER_M
            + total_output_tokens * TEXT_OUTPUT_RATE_PER_M) / 1_000_000

    summary = {
        "effort": effort,
        "retention_pass": retention_pass,
        "final_reply": final_reply,
        "total_elapsed_s": total_elapsed,
        "p50_latency_s": p50_latency,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_reasoning_tokens": total_reasoning_tokens if reasoning_split_available else None,
        "cost_usd": cost,
    }

    print(f"  --- summary: effort={effort} retention={'PASS' if retention_pass else 'FAIL'} "
          f"total={total_elapsed:.2f}s p50={p50_latency:.2f}s "
          f"tokens(in/out)={total_input_tokens}/{total_output_tokens} "
          f"reasoning_tokens={'n/a' if not reasoning_split_available else total_reasoning_tokens} "
          f"cost=${cost:.5f}")
    return summary


async def run_poc():
    config = load_settings()
    results = []
    for effort in REASONING_EFFORTS:
        summary = await run_effort_level(effort, config)
        results.append(summary)

    print("\n=== FINAL COMBINED TABLE ===")
    header = f"{'effort':<8} {'retention':<10} {'total_s':<9} {'p50_s':<8} {'in_tok':<8} {'out_tok':<8} {'reason_tok':<11} {'cost_usd':<10}"
    print(header)
    print("-" * len(header))
    for r in results:
        reasoning_str = "n/a" if r["total_reasoning_tokens"] is None else str(r["total_reasoning_tokens"])
        print(f"{r['effort']:<8} {'PASS' if r['retention_pass'] else 'FAIL':<10} "
              f"{r['total_elapsed_s']:<9.2f} {r['p50_latency_s']:<8.2f} "
              f"{r['total_input_tokens']:<8} {r['total_output_tokens']:<8} "
              f"{reasoning_str:<11} {r['cost_usd']:<10.5f}")

    return results


if __name__ == "__main__":
    asyncio.run(run_poc())
