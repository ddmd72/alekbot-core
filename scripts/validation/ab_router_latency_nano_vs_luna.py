#!/usr/bin/env python3
"""
Router latency A/B — gpt-5.4-nano (ECO)  vs  gpt-5.6-luna (BALANCED)
====================================================================
Replays REAL router triage calls on both models and reports wall-clock latency.

Why this exists
---------------
OpenAI's 2026-07-30 price cut erased the ECO/BALANCED price gap: nano is $0.20/$1.25,
Luna $0.20/$1.20 — same input price, Luna marginally cheaper on output. Keeping nano on
ECO is therefore a latency argument, and nothing else. This measures that argument.

Why production telemetry cannot answer it: the tier mapping guarantees the two models
never do the same work. Over 7 days the only agent that ran on both was `web_search`,
where nano served `fetch_url` and Luna served grounded `search_web` — different tasks,
one of them including server-side web fetching. Comparing those averages is meaningless.

Why the router workload: it is ECO in production and it sits on the critical path of every
single message, so its latency is the part the user actually feels. It is also the cheapest
realistic shape to replay (~1.8K uncached prompt tokens, JSON out, no tools, no thinking).

Faithfulness
------------
Prompts are the real ones, pulled from the BigQuery content store and split back into
system instruction + messages. Requests go through the production `OpenAIAdapter` with the
same parameters `RouterAgent._classify` sends (temperature, max_tokens=300, disable_safety,
response_mime_type=json, response_schema). Only `model_name` differs between legs.

TTFT is deliberately NOT measured: we do not stream, the user waits for the whole response,
so total call latency is the number that matters. Public benchmarks quoting TTFT do not
transfer to this system.

Method
------
Legs alternate per repeat (nano, luna, luna, nano — mirrored) so a drift in API latency
hits both models evenly; calls are sequential for the same reason. The first repeat of each
prompt is discarded as warm-up (prompt caching makes call 1 unrepresentative of steady
state — which is what production sees, since the router prompt is cached per user).

NOTE: real LLM calls on BOTH models — spends API budget. A default run
(6 prompts x 4 repeats x 2 models = 48 calls) costs well under $0.05.

Usage:
    python scripts/validation/ab_router_latency_nano_vs_luna.py
    python scripts/validation/ab_router_latency_nano_vs_luna.py --prompts 10 --repeats 6
    python scripts/validation/ab_router_latency_nano_vs_luna.py --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Bypass a local MITM proxy (Charles registers itself as the macOS system proxy, which
# httpx picks up via getproxies() even with no *_PROXY env var set → "Connection error"
# on every SDK call while curl works). Must be set before the OpenAI client is built.
os.environ.setdefault("NO_PROXY", "*")

from dotenv import load_dotenv

load_dotenv()

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.billing import calculate_cost
from src.domain.llm import LLMRequest, Message, MessagePart

NANO = "gpt-5.4-nano"
LUNA = "gpt-5.6-luna"

# Mirrors RouterAgent._classify (src/agents/core/router_agent.py) — keep in sync or the
# measurement stops describing production.
TEMPERATURE = 0.1
MAX_TOKENS = 300
_SYSTEM_MARK = "=== SYSTEM ==="
_MESSAGES_MARK = "=== MESSAGES ==="


# --------------------------------------------------------------------------- #
# Input: real router calls from the BigQuery content store
# --------------------------------------------------------------------------- #

def load_router_calls(days: int, limit: int, project: str, dataset: str) -> List[Dict[str, Any]]:
    """Pull recent router turns and split request_text back into system + messages.

    The store renders one blob (`_serialize_request`); this is its inverse. Rows whose
    blob lacks either marker are skipped rather than guessed at.
    """
    # `bq` CLI rather than the python client: google-cloud-bigquery is not a runtime
    # dependency of this repo, and the sibling harness (scripts/websearch) does the same.
    import subprocess

    sql = f"""
        SELECT request_text, prompt_tokens, completion_tokens
        FROM `{project}.{dataset}.prompt_content`
        WHERE agent_type LIKE '%router%'
          AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        ORDER BY timestamp DESC
    """
    out = subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", f"--max_rows={limit * 3}", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    calls: List[Dict[str, Any]] = []
    for row in json.loads(out):
        blob = row["request_text"] or ""
        if _SYSTEM_MARK not in blob or _MESSAGES_MARK not in blob:
            continue
        system, _, rest = blob.partition(_MESSAGES_MARK)
        system = system.replace(_SYSTEM_MARK, "", 1).strip()
        user_text = rest.strip()
        if not system or not user_text:
            continue
        calls.append({
            "system": system,
            "user": user_text,
            "prod_prompt_tokens": int(row["prompt_tokens"] or 0),
        })
        if len(calls) >= limit:
            break
    return calls


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

async def run_leg(adapter: OpenAIAdapter, model: str, call: Dict[str, Any],
                  schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    request = LLMRequest(
        model_name=model,
        system_instruction=call["system"],
        messages=[Message(role="user", parts=[MessagePart(text=call["user"])])],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        disable_safety=True,
        response_mime_type="application/json",
        response_schema=schema,
    )
    t0 = time.perf_counter()
    try:
        resp = await adapter.generate_content(request)
    except Exception as e:
        return {"model": model, "error": f"{type(e).__name__}: {e}",
                "latency_s": time.perf_counter() - t0}
    latency = time.perf_counter() - t0
    u = resp.usage_metadata
    text = resp.text or ""
    result = {
        "model": model,
        "latency_s": latency,
        "prompt_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) if u else 0,
        "cache_read_tokens": getattr(u, "cache_read_tokens", 0) if u else 0,
        "parsed": _is_valid_triage(text),
        "text_chars": len(text),
        # Hidden reasoning tokens: completion_tokens minus what the visible text can
        # account for (~4 chars/token for this JSON). A model that reasons by default
        # burns the max_tokens budget the router sets, and truncates its own answer.
        "text_tail": text[-60:] if text else "",
    }
    result["cost"] = calculate_cost(
        model, result["prompt_tokens"], result["completion_tokens"],
        cache_read_tokens=result["cache_read_tokens"],
    )
    return result


def _is_valid_triage(text: str) -> bool:
    """A fast model that is quicker but returns unparseable JSON has not won anything."""
    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, AttributeError):
        return False
    return isinstance(parsed, dict) and bool(parsed)


async def measure(adapter: OpenAIAdapter, calls: List[Dict[str, Any]], repeats: int,
                  schema: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for i, call in enumerate(calls):
        for r in range(repeats):
            # Mirror the order on alternate repeats so a latency drift over the run
            # cannot systematically favour whichever model always goes first.
            order = (NANO, LUNA) if r % 2 == 0 else (LUNA, NANO)
            for model in order:
                res = await run_leg(adapter, model, call, schema)
                res["prompt_idx"] = i
                res["repeat"] = r
                res["warmup"] = (r == 0)
                rows.append(res)
                flag = "" if res.get("parsed") else "  ⚠ unparseable"
                err = res.get("error", "")
                print(f"  [{i}:{r}] {model:14s} {res['latency_s']:5.2f}s{flag}{err}")
    return rows


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def _pct(values: List[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(int(q * len(ordered)), len(ordered) - 1)
    return ordered[idx]


def report(rows: List[Dict[str, Any]]) -> None:
    steady = [r for r in rows if not r["warmup"] and "error" not in r]
    print("\n" + "=" * 72)
    print(f"Steady-state calls: {len(steady)} (warm-up repeat discarded)")
    errors = [r for r in rows if "error" in r]
    if errors:
        print(f"Errors: {len(errors)} — {errors[0]['error']}")

    summary: Dict[str, Dict[str, float]] = {}
    for model in (NANO, LUNA):
        legs = [r for r in steady if r["model"] == model]
        if not legs:
            continue
        lat = [r["latency_s"] for r in legs]
        completion = [r["completion_tokens"] for r in legs]
        per_token = [r["latency_s"] * 1000 / c for r, c in zip(legs, completion) if c]
        summary[model] = {
            "n": len(legs),
            "mean": statistics.mean(lat),
            "p50": statistics.median(lat),
            "p95": _pct(lat, 0.95),
            "ms_per_token": statistics.mean(per_token) if per_token else 0.0,
            "completion": statistics.mean(completion) if completion else 0.0,
            "cost": sum(r["cost"] for r in legs),
            "parsed": sum(1 for r in legs if r["parsed"]) / len(legs),
        }

    print(f"\n{'model':16s} {'n':>4s} {'mean':>7s} {'p50':>7s} {'p95':>7s} "
          f"{'ms/tok':>8s} {'out tok':>8s} {'valid':>7s} {'cost':>9s}")
    for model, s in summary.items():
        print(f"{model:16s} {s['n']:4.0f} {s['mean']:6.2f}s {s['p50']:6.2f}s {s['p95']:6.2f}s "
              f"{s['ms_per_token']:8.1f} {s['completion']:8.0f} {s['parsed']:6.0%} "
              f"${s['cost']:8.4f}")

    # Why a model is slow matters as much as that it is: at `thinking=None` the adapter
    # sends no `reasoning.effort`, so each model runs at its API default. Completion
    # tokens far above what the visible text can hold = the default is not "none".
    for model in (NANO, LUNA):
        legs = [r for r in steady if r["model"] == model]
        if not legs:
            continue
        visible = statistics.mean(r["text_chars"] for r in legs) / 4  # ~4 chars/token
        emitted = statistics.mean(r["completion_tokens"] for r in legs)
        bad = [r for r in legs if not r["parsed"]]
        print(f"\n{model}: {emitted:.0f} completion tokens vs ~{visible:.0f} the text "
              f"accounts for → ~{max(emitted - visible, 0):.0f} hidden/reasoning")
        if bad:
            print(f"  {len(bad)} unparseable, e.g. ...{bad[0]['text_tail']!r}")

    if len(summary) == 2:
        nano_s, luna_s = summary[NANO], summary[LUNA]
        delta = (luna_s["p50"] - nano_s["p50"]) / nano_s["p50"] * 100
        faster = LUNA if delta < 0 else NANO
        print(f"\nMedian delta: {abs(delta):.1f}% in favour of {faster}")
        # Paired comparison: same prompt, same repeat — removes prompt-length variance.
        pairs = {}
        for r in steady:
            pairs.setdefault((r["prompt_idx"], r["repeat"]), {})[r["model"]] = r["latency_s"]
        both = [v for v in pairs.values() if len(v) == 2]
        wins = sum(1 for v in both if v[LUNA] < v[NANO])
        print(f"Paired: luna faster in {wins}/{len(both)} matched calls")
        print("\nRead this against the price table: at $0.20/$1.25 (nano) vs $0.20/$1.20 "
              "(luna)\ncost is a wash, so a latency tie means the tiers are interchangeable "
              "and ECO\nshould simply move to luna (one generation, one deprecation clock).")


# --------------------------------------------------------------------------- #

async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prompts", type=int, default=6, help="distinct router prompts to replay")
    parser.add_argument("--repeats", type=int, default=4, help="runs per prompt per model (first discarded)")
    parser.add_argument("--days", type=int, default=3, help="how far back to pull prompts")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    parser.add_argument("--dataset", default="alek_observability_dev")
    args = parser.parse_args()

    if not args.project:
        sys.exit("GOOGLE_CLOUD_PROJECT is not set (.env) and --project was not given")

    print(f"Loading up to {args.prompts} router prompts from the last {args.days} day(s)...")
    calls = load_router_calls(args.days, args.prompts, args.project, args.dataset)
    if not calls:
        sys.exit("No router rows found in that window — widen --days")
    print(f"  {len(calls)} prompts "
          f"(avg {statistics.mean(c['prod_prompt_tokens'] for c in calls):.0f} uncached prompt tokens)")

    try:
        from src.agents.core.router_agent import RouterAgent
        schema = RouterAgent.TRIAGE_RESPONSE_SCHEMA
    except Exception as e:  # schema is a hint, not the measurement — keep going without it
        print(f"  ⚠ could not import the router schema ({e}); running without response_schema")
        schema = None

    adapter = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
    total = len(calls) * args.repeats * 2
    print(f"Running {total} calls ({args.repeats} repeats x 2 models, alternating order)...\n")
    rows = await measure(adapter, calls, args.repeats, schema)
    report(rows)


if __name__ == "__main__":
    asyncio.run(main())
