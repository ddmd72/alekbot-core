#!/usr/bin/env python3
"""
WebSearch A/B — gpt-5.4-nano (ECO)  vs  gpt-5.6-luna (BALANCED, current default)
================================================================================
Replays the REAL web_search calls from one morning-briefing run on both models and
prints them side by side.

Why: the briefing's 40 grounded calls are ~73% plain `fetch_url` work (154-char system
prompt, "return the page's last-24h items, no JSON") — a task that plausibly does not
need a BALANCED model. nano is 5x cheaper on input, 4.8x on output.

Faithfulness: the request is rebuilt from the recorded prompt and issued through the
production `OpenAIAdapter` with `use_grounding=True` — the same code path, tool wiring
and parameter gating the agent uses. Only `model_name` differs between the two legs.
Both legs for a given query run back-to-back so they see the same web state.

Two intents are measured separately — they have different failure modes:
  • fetch_url  (31/40 calls) — plain text expected; system prompt forbids JSON/fences.
  • search_web ( 9/40 calls) — JSON expected from the OUTPUT_FORMAT token ALONE, because
                 the adapter suppresses response_schema whenever grounding is on
                 (Web Search + JSON mode → 400). This is the hard gate for a small model.

NOTE: real LLM calls on BOTH models — spends API budget (~$1.4 for a full 40-query run).
Run deliberately. Use --limit / --intent to sample.

Usage:
    python scripts/websearch/ab_nano_vs_luna.py --date 2026-07-29
    python scripts/websearch/ab_nano_vs_luna.py --intent fetch_url --limit 8
    python scripts/websearch/ab_nano_vs_luna.py --intent search_web       # the risky 9
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Bypass a local MITM proxy (Charles registers itself as the macOS system proxy, which
# httpx picks up via getproxies() even with no *_PROXY env var set → "Connection error"
# on every SDK call while curl works). Must be set before the OpenAI client is built.
# setdefault: an explicit NO_PROXY still wins.
os.environ.setdefault("NO_PROXY", "*")

from dotenv import load_dotenv

load_dotenv()

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.billing import calculate_cost
from src.domain.llm import LLMRequest, Message, MessagePart

NANO = "gpt-5.4-nano"
LUNA = "gpt-5.6-luna"
FETCH_MARKER = "Fetch the provided URL"
OUT_DIR = Path(__file__).parent.parent / "memory"


# --------------------------------------------------------------------------- #
# Input: the real calls, pulled from the BigQuery content store
# --------------------------------------------------------------------------- #

def load_recorded_calls(date: str, project: str, dataset: str) -> List[Dict[str, Any]]:
    """Pull one day's web_search turns (prompt + what luna actually returned)."""
    import subprocess

    sql = f"""
    SELECT span_id, request_text, response_text,
           prompt_tokens, completion_tokens, cache_read_tokens, latency_ms
    FROM `{project}.{dataset}.prompt_content`
    WHERE agent_type = "web_search" AND DATE(timestamp) = "{date}"
    ORDER BY timestamp
    """
    out = subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=200", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = json.loads(out)

    calls = []
    for r in rows:
        system, user = _split_prompt(r["request_text"])
        if not user:
            continue
        calls.append({
            "span_id": r["span_id"],
            "intent": "fetch_url" if FETCH_MARKER in system else "search_web",
            "system": system,
            "user": user,
            "prod_response": r["response_text"] or "",
            "prod_cost": calculate_cost(
                LUNA, int(r["prompt_tokens"]), int(r["completion_tokens"]),
                cache_read_tokens=int(r["cache_read_tokens"]),
            ),
            "prod_latency_s": float(r["latency_ms"]) / 1000.0,
        })
    return calls


def _split_prompt(request_text: str) -> tuple[str, str]:
    """Recover (system_instruction, user_message) from the stored transcript."""
    sys_m = re.search(r"=== SYSTEM ===\n(.*?)\n\n=== MESSAGES ===", request_text, re.S)
    usr_m = re.search(r"^user: (.*)$", request_text, re.S | re.M)
    return (sys_m.group(1).strip() if sys_m else "",
            usr_m.group(1).strip() if usr_m else "")


# --------------------------------------------------------------------------- #
# Scoring — cheap, deterministic, no LLM judge
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


def score(text: str, intent: str) -> Dict[str, Any]:
    """Structural quality signals. Deliberately mechanical: an LLM judge would add
    cost and its own variance to a question that is mostly about format compliance
    and how much verifiable substance came back."""
    urls = _URL_RE.findall(text or "")
    s: Dict[str, Any] = {
        "chars": len(text or ""),
        "urls": len(urls),
        "distinct_domains": len({u.split("/")[2] for u in urls if len(u.split("/")) > 2}),
        "empty": not (text or "").strip(),
    }
    if intent == "fetch_url":
        # The system prompt explicitly forbids JSON and code blocks.
        s["violates_no_json"] = bool(
            "```" in text or (text or "").lstrip().startswith(("{", "["))
        )
    else:
        s["json_valid"] = _json_parses(text)
    return s


def _json_parses(text: str) -> bool:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        json.loads(raw)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

async def run_leg(adapter: OpenAIAdapter, model: str, call: Dict[str, Any]) -> Dict[str, Any]:
    request = LLMRequest(
        model_name=model,
        system_instruction=call["system"],
        messages=[Message(role="user", parts=[MessagePart(text=call["user"])])],
        use_grounding=True,
    )
    t0 = time.perf_counter()
    try:
        resp = await adapter.generate_content(request)
    except Exception as e:
        return {"model": model, "error": f"{type(e).__name__}: {e}",
                "latency_s": time.perf_counter() - t0}
    latency = time.perf_counter() - t0
    u = resp.usage_metadata
    result = {
        "model": model,
        "text": resp.text or "",
        "latency_s": latency,
        "grounded": resp.grounding_metadata is not None,
        "prompt_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) if u else 0,
        "cache_read_tokens": getattr(u, "cache_read_tokens", 0) if u else 0,
    }
    result["cost"] = calculate_cost(
        model, result["prompt_tokens"], result["completion_tokens"],
        cache_read_tokens=result["cache_read_tokens"],
    )
    result.update(score(result["text"], call["intent"]))
    return result


async def run_pair(adapter: OpenAIAdapter, call: Dict[str, Any], sem: asyncio.Semaphore
                   ) -> Dict[str, Any]:
    async with sem:
        # Back-to-back, not concurrent: same web state, and the shared semaphore keeps
        # total in-flight requests bounded.
        nano = await run_leg(adapter, NANO, call)
        luna = await run_leg(adapter, LUNA, call)
    return {"call": call, NANO: nano, LUNA: luna}


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _mean(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def report(pairs: List[Dict[str, Any]]) -> None:
    for intent in ("fetch_url", "search_web"):
        subset = [p for p in pairs if p["call"]["intent"] == intent]
        if not subset:
            continue
        print(f"\n{'=' * 100}\n{intent.upper()}  —  {len(subset)} calls\n{'=' * 100}")
        print(f"{'#':<3} {'query':<42} {'nano':>26}   {'luna':>26}")
        print(f"{'':<3} {'':<42} {'$/s/urls/chars':>26}   {'$/s/urls/chars':>26}")
        for i, p in enumerate(subset, 1):
            label = _label(p["call"])
            print(f"{i:<3} {label:<42} {_cell(p[NANO]):>26}   {_cell(p[LUNA]):>26}")

        for tag, model in (("nano", NANO), ("luna", LUNA)):
            legs = [p[model] for p in subset]
            ok = [x for x in legs if "error" not in x]
            print(f"\n  {tag:<5} cost=${sum(x['cost'] for x in ok):.4f}"
                  f"  avg_latency={_mean([x['latency_s'] for x in ok]):5.1f}s"
                  f"  avg_urls={_mean([x['urls'] for x in ok]):5.1f}"
                  f"  avg_chars={_mean([x['chars'] for x in ok]):7.0f}"
                  f"  grounded={sum(1 for x in ok if x['grounded'])}/{len(ok)}"
                  f"  empty={sum(1 for x in ok if x['empty'])}"
                  f"  errors={len(legs) - len(ok)}")
            if intent == "fetch_url":
                print(f"        format violations (JSON/fences): "
                      f"{sum(1 for x in ok if x.get('violates_no_json'))}/{len(ok)}")
            else:
                print(f"        JSON parses: {sum(1 for x in ok if x.get('json_valid'))}/{len(ok)}")

        prod = [p["call"]["prod_cost"] for p in subset]
        print(f"\n  production (luna @ 06:05): cost=${sum(prod):.4f}"
              f"  avg_latency={_mean([p['call']['prod_latency_s'] for p in subset]):5.1f}s")

    n_cost = sum(p[NANO]["cost"] for p in pairs if "error" not in p[NANO])
    l_cost = sum(p[LUNA]["cost"] for p in pairs if "error" not in p[LUNA])
    print(f"\n{'=' * 100}")
    print(f"TOTAL   nano=${n_cost:.4f}   luna=${l_cost:.4f}"
          f"   ratio={l_cost / n_cost if n_cost else 0:.2f}x"
          f"   saving=${l_cost - n_cost:.4f}/run  (~${(l_cost - n_cost) * 30:.2f}/month)")


def _label(call: Dict[str, Any]) -> str:
    urls = _URL_RE.findall(call["user"])
    if urls:
        return urls[0].split("//")[-1][:41]
    return " ".join(call["user"].split())[:41]


def _cell(leg: Dict[str, Any]) -> str:
    if "error" in leg:
        return "ERROR"
    flag = ""
    if leg.get("violates_no_json"):
        flag = " !fmt"
    elif leg.get("json_valid") is False:
        flag = " !json"
    elif leg["empty"]:
        flag = " !empty"
    return f"${leg['cost']:.4f} {leg['latency_s']:4.1f}s {leg['urls']:2d}u {leg['chars']:5d}c{flag}"


async def main() -> None:
    ap = argparse.ArgumentParser(description="WebSearch A/B: gpt-5.4-nano vs gpt-5.6-luna")
    ap.add_argument("--date", default=None, help="UTC date of the briefing run (default: today)")
    ap.add_argument("--intent", choices=["fetch_url", "search_web", "both"], default="both")
    ap.add_argument("--limit", type=int, default=0, help="cap calls per intent (0 = all)")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    date = args.date or time.strftime("%Y-%m-%d", time.gmtime())
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.getenv("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")

    calls = load_recorded_calls(date, project, dataset)
    if args.intent != "both":
        calls = [c for c in calls if c["intent"] == args.intent]
    if args.limit:
        kept: List[Dict[str, Any]] = []
        for intent in ("fetch_url", "search_web"):
            kept += [c for c in calls if c["intent"] == intent][: args.limit]
        calls = kept
    if not calls:
        print(f"No web_search calls recorded for {date}")
        return

    n_fetch = sum(1 for c in calls if c["intent"] == "fetch_url")
    print(f"Replaying {len(calls)} recorded calls from {date} "
          f"({n_fetch} fetch_url, {len(calls) - n_fetch} search_web) on {NANO} and {LUNA}")

    adapter = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(args.concurrency)
    pairs = await asyncio.gather(*[run_pair(adapter, c, sem) for c in calls])

    report(list(pairs))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dump = OUT_DIR / f"websearch_ab_{date}.json"
    dump.write_text(json.dumps(pairs, indent=2, ensure_ascii=False))
    print(f"\nFull outputs (both legs, verbatim) → {dump}")


if __name__ == "__main__":
    asyncio.run(main())
