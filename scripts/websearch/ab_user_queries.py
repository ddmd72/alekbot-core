#!/usr/bin/env python3
"""
search_web A/B on REAL user queries — gpt-5.4-nano (ECO) vs gpt-5.6-luna
========================================================================
The `fetch_url` measurements (ab_nano_vs_luna.py, tune_fetch_prompt.py) covered only the
morning briefing. `search_web` is a different job with a different prompt: the full
Firestore-assembled WebSearchAgent prompt (~3.2k chars, QUICK/RESEARCH triage,
OUTPUT_FORMAT) instead of a 154-char inline string.

It also has a different failure mode. `WebSearchAgent` has NO `_parse_response` — the
grounded reply is handed to the orchestrator verbatim, so malformed JSON never crashes
anything. What actually hurts is an EMPTY finding set (measured on briefing queries:
nano returned `findings: []` on 4 of 9) or a confident answer that is simply wrong.
The second cannot be scored mechanically, so this script prints both answers in full
for reading and flags divergence for review.

Note `confidence = min(1.0, len(result_text) / 500)` in `_call_grounded_llm`: a terse
answer is automatically low-confidence downstream, so answer length is not merely
cosmetic here.

Fidelity: replays the recorded system prompt (real Firestore assembly) through the
production OpenAIAdapter, with `current_date_time` refreshed to now — production builds
that line per call, and half these queries are time-sensitive ("as of today", "latest
official status").

NOTE: real LLM calls on BOTH models. ~$0.30 for the default 7 queries.

Usage:
    python scripts/websearch/ab_user_queries.py
    python scripts/websearch/ab_user_queries.py --only wizz,wildfire
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# See ab_nano_vs_luna.py — bypass the MITM proxy inherited from macOS system settings.
os.environ.setdefault("NO_PROXY", "*")

from dotenv import load_dotenv

load_dotenv()

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.billing import calculate_cost
from src.domain.llm import LLMRequest, Message, MessagePart

NANO = "gpt-5.4-nano"
LUNA = "gpt-5.6-luna"

# Curated from 331 distinct recorded user-driven search_web queries. Chosen for spread of
# difficulty and kind, not for flattering either model:
#   legal/regulatory in Spanish (highest damage if confidently wrong), local commerce,
#   live current events, official procedural rules, and one trivial lookup as a floor.
SELECTORS: Dict[str, str] = {
    "wizz":      "Wizz Air official rules",
    "wildfire":  "La Vall d'Uixó wildfire",
    "jeweller":  "jewellery repair services in Pu",
    "repair_biz": "jewellery repair broken gold bracelet clasp",
    "sem_instr": "SEM 2/2026 instrucci",
    "visa":      "highly qualified worker visa Ley 14/2013",
    "weather":   "Weather today",
}

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")
_DATE_LINE = re.compile(r"^current_date_time: .*$", re.M)


def fetch_recorded(project: str, dataset: str) -> List[Dict[str, Any]]:
    sql = f"""
    SELECT timestamp, request_text, response_text, prompt_tokens, completion_tokens,
           cache_read_tokens, latency_ms
    FROM `{project}.{dataset}.prompt_content`
    WHERE agent_type = "web_search" AND request_text LIKE "%WebSearchAgent extends Agent%"
    ORDER BY timestamp DESC
    """
    out = subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=400", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def split_prompt(request_text: str) -> tuple[str, str]:
    s = re.search(r"=== SYSTEM ===\n(.*?)\n\n=== MESSAGES ===", request_text, re.S)
    u = re.search(r"^user: (.*)$", request_text, re.S | re.M)
    return (s.group(1).strip() if s else "", u.group(1).strip() if u else "")


def refresh_date(system: str) -> str:
    """Production assembles current_date_time per call; the recording froze it."""
    now = datetime.now(timezone.utc).strftime("%A, %d %B %Y, %H:%M UTC")
    return _DATE_LINE.sub(f"current_date_time: {now}", system)


def pick(rows: List[Dict[str, Any]], wanted: List[str]) -> Dict[str, Dict[str, str]]:
    """Newest recorded call per selector (each has a real assembled prompt)."""
    out: Dict[str, Dict[str, str]] = {}
    for name in wanted:
        needle = SELECTORS[name]
        for r in rows:
            system, user = split_prompt(r["request_text"])
            if needle.lower() in user.lower():
                out[name] = {
                    "system": refresh_date(system),
                    "user": re.sub(r"^\[\w+ \d+, [\d:]+ UTC\]\s*", "", user),
                    "recorded_at": r["timestamp"][:16],
                    "recorded_response": r["response_text"] or "",
                }
                break
    return out


async def run(adapter: OpenAIAdapter, model: str, case: Dict[str, str],
              sem: asyncio.Semaphore) -> Dict[str, Any]:
    async with sem:
        t0 = time.perf_counter()
        try:
            resp = await adapter.generate_content(LLMRequest(
                model_name=model,
                system_instruction=case["system"],
                messages=[Message(role="user", parts=[MessagePart(text=case["user"])])],
                use_grounding=True,
            ))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "text": "", "urls": 0,
                    "chars": 0, "findings": 0, "cost": 0.0,
                    "latency_s": time.perf_counter() - t0}
        text = resp.text or ""
        u = resp.usage_metadata
        return {
            "text": text,
            "latency_s": time.perf_counter() - t0,
            "urls": len(_URL_RE.findall(text)),
            "chars": len(text),
            "findings": count_findings(text),
            # Mirrors _call_grounded_llm: length drives the confidence the orchestrator sees.
            "confidence": min(1.0, len(text) / 500),
            "cost": calculate_cost(
                model,
                getattr(u, "prompt_tokens", 0) if u else 0,
                getattr(u, "completion_tokens", 0) if u else 0,
                cache_read_tokens=getattr(u, "cache_read_tokens", 0) if u else 0,
            ),
        }


def count_findings(text: str) -> int:
    """Findings in the OUTPUT_FORMAT shape; falls back to 0 when unparseable.

    Unparseable is NOT a failure here — nothing in production parses this — but an empty
    or missing findings list IS: the orchestrator then has nothing to compose from.
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return 0
    f = data.get("findings") if isinstance(data, dict) else None
    return len(f) if isinstance(f, list) else 0


async def main() -> None:
    ap = argparse.ArgumentParser(description="search_web A/B on real user queries")
    ap.add_argument("--only", default="", help="comma-separated selector names")
    ap.add_argument("--concurrency", type=int, default=4)
    args = ap.parse_args()

    wanted = [n.strip() for n in args.only.split(",") if n.strip()] or list(SELECTORS)
    unknown = [n for n in wanted if n not in SELECTORS]
    if unknown:
        raise SystemExit(f"unknown selectors {unknown}; known: {list(SELECTORS)}")

    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.getenv("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")
    cases = pick(fetch_recorded(project, dataset), wanted)
    missing = [n for n in wanted if n not in cases]
    if missing:
        print(f"⚠️  no recorded call found for: {missing}")
    if not cases:
        return

    adapter = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(args.concurrency)

    names = list(cases)
    legs = await asyncio.gather(*[
        run(adapter, m, cases[n], sem) for n in names for m in (NANO, LUNA)
    ])
    results = {n: {NANO: legs[i * 2], LUNA: legs[i * 2 + 1]} for i, n in enumerate(names)}

    print(f"\n{'=' * 100}")
    print(f"{'query':<12} {'nano: $/s/find/urls/chars':>38} {'luna: $/s/find/urls/chars':>38}")
    print(f"{'=' * 100}")
    for n in names:
        def cell(r: Dict[str, Any]) -> str:
            if "error" in r:
                return "ERROR"
            return (f"${r['cost']:.4f} {r['latency_s']:5.1f}s {r['findings']:2d}f "
                    f"{r['urls']:2d}u {r['chars']:5d}c"
                    + ("  EMPTY" if r["findings"] == 0 and r["urls"] == 0 else ""))
        print(f"{n:<12} {cell(results[n][NANO]):>38} {cell(results[n][LUNA]):>38}")

    for tag, m in (("nano", NANO), ("luna", LUNA)):
        ok = [results[n][m] for n in names if "error" not in results[n][m]]
        k = len(ok) or 1
        dead = sum(1 for r in ok if r["findings"] == 0 and r["urls"] == 0)
        print(f"\n{tag:<5} cost=${sum(r['cost'] for r in ok):.4f}"
              f"  avg_findings={sum(r['findings'] for r in ok) / k:4.1f}"
              f"  avg_urls={sum(r['urls'] for r in ok) / k:4.1f}"
              f"  avg_chars={sum(r['chars'] for r in ok) / k:6.0f}"
              f"  avg_conf={sum(r['confidence'] for r in ok) / k:4.2f}"
              f"  answered_nothing={dead}/{len(ok)}"
              f"  avg_lat={sum(r['latency_s'] for r in ok) / k:5.1f}s")

    out = Path(__file__).parent.parent / "memory" / "websearch_user_queries_ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {n: {"query": cases[n]["user"], "recorded_at": cases[n]["recorded_at"],
             "nano": results[n][NANO], "luna": results[n][LUNA]} for n in names},
        indent=2, ensure_ascii=False))
    print(f"\nBoth answers in full (for reading — quality here needs judgement) → {out}")


if __name__ == "__main__":
    asyncio.run(main())
