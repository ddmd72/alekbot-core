#!/usr/bin/env python3
"""
Router A/B — OpenAI nano vs Gemini Flash-Lite, on the user's own historical prompts
==================================================================================
Replays REAL router requests pulled from BigQuery `prompt_content` through both
providers via the production adapters, and reports the three things that decide
this choice: decision quality, stability, and latency.

Why real prompts: a synthetic triage says nothing about how the router behaves on
this user's actual traffic — the distribution of tones, languages, and complexity
is the whole point. The corpus is every distinct router request still inside
BigQuery's 30-day TTL (227 as of 2026-08-15, oldest 2026-07-16).

MODELS ARE PINNED EXPLICITLY, never `-latest`. An alias can be repointed by the
provider between the measurement and the decision it justifies, which would make
this report quietly wrong. `gemini-flash-lite-latest` currently resolves to one of
six flash-lite models; the explicit ID is measured instead so the number stays
attached to the thing that produced it.

Three metrics, three different questions:

  latency     p50/p95 wall time per call. Non-streaming: the router waits for the
              complete JSON, so TTFT (what public benchmarks publish) is not the
              number that lands on a user's message.

  stability   TWO senses, both reported. (a) parse validity — Flash-Lite has a
              documented failure mode of returning EMPTY responses under
              schema + Groovy-DSL prompts (src/adapters/CLAUDE.md, 22+ tests);
              this is the run that would catch it. (b) self-consistency — the same
              prompt run N times: does the model give the same triage each time?
              An unstable router makes the same message behave differently.

  quality     There is no ground truth for a triage, so this reports AGREEMENT,
              not correctness: cross-provider agreement per field, and agreement
              with what production actually answered. Every disagreement is dumped
              to scripts/memory/ for human review — that dump IS the quality
              signal; the percentages only tell you where to look.

`metadata.task_complexity` is called out separately: it selects Smart's tier, so a
disagreement there costs money and changes behaviour, unlike a differing
`search_phrase`.

Cost: prompts x repeats x 2 legs real calls, ~2k input / ~150 output each.
The default 25 x 3 is ~150 calls, well under $0.10 total. Runs sequentially and
interleaved — parallelism would corrupt the latency measurement.

Usage:
    python scripts/validation/ab_router_gemini_vs_openai.py
    python scripts/validation/ab_router_gemini_vs_openai.py --limit 50 --repeats 3
    python scripts/validation/ab_router_gemini_vs_openai.py --gemini-model gemini-3.1-flash-lite
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()

from openai import AsyncOpenAI

from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.agents.core.router_agent import RouterAgent
from src.domain.billing import calculate_cost
from src.ports.llm_port import LLMRequest, Message, MessagePart

OUT_DIR = Path(__file__).parent.parent / "memory"
BQ_TABLE = "gen-lang-client-0554950952.alek_observability_dev.prompt_content"

# Fields compared between legs. metadata.* is flattened to dotted keys.
FIELDS = [
    "needs_memory_search",
    "search_intent",
    "relevant_domains",
    "semantic_lens",
    "search_phrase",
    "metadata.user_tone",
    "metadata.task_complexity",
]
# Disagreement here changes Smart's tier — i.e. cost and behaviour, not just wording.
CONSEQUENTIAL = {"metadata.task_complexity", "needs_memory_search", "search_intent"}
# Free-text; models will phrase it differently without being "wrong". Reported, not judged.
FREE_TEXT = {"search_phrase"}

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Triage the request per the instructions.")])]


# --------------------------------------------------------------------------- #
# Corpus                                                                       #
# --------------------------------------------------------------------------- #

def load_corpus(limit: int) -> List[Dict[str, Any]]:
    """Distinct real router requests + what production answered at the time."""
    import subprocess

    sql = f"""
        SELECT request_text, response_text, model, timestamp
        FROM `{BQ_TABLE}`
        WHERE agent_type LIKE "%router%"
          AND request_text IS NOT NULL AND LENGTH(request_text) > 500
        QUALIFY ROW_NUMBER() OVER (PARTITION BY request_text ORDER BY timestamp DESC) = 1
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    out = subprocess.run(
        ["bq", "query", "--project_id=gen-lang-client-0554950952",
         "--use_legacy_sql=false", "--format=json", sql],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


# --------------------------------------------------------------------------- #
# Comparison helpers                                                           #
# --------------------------------------------------------------------------- #

def flatten(triage: Optional[dict]) -> Dict[str, Any]:
    if not isinstance(triage, dict):
        return {}
    flat = {}
    for f in FIELDS:
        if "." in f:
            outer, inner = f.split(".", 1)
            flat[f] = (triage.get(outer) or {}).get(inner) if isinstance(triage.get(outer), dict) else None
        else:
            flat[f] = triage.get(f)
    return flat


def canon(value: Any) -> Any:
    """Order-insensitive, case-insensitive comparison key."""
    if isinstance(value, list):
        return tuple(sorted(str(v).strip().lower() for v in value))
    if isinstance(value, str):
        return value.strip().lower()
    return value


def modal(values: List[Any]) -> Any:
    """Most common value across repeats — the leg's 'answer' for a prompt."""
    if not values:
        return None
    counts = Counter(canon(v) for v in values)
    top = counts.most_common(1)[0][0]
    for v in values:
        if canon(v) == top:
            return v
    return values[0]


def parse(text: str) -> Optional[dict]:
    try:
        return json.loads((text or "").strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #

def build_request(model: str, prompt: str) -> LLMRequest:
    """Exactly the shape RouterAgent builds (router_agent.py:467-476)."""
    return LLMRequest(
        model_name=model,
        system_instruction=prompt,
        messages=_MESSAGES,
        temperature=RouterAgent.TEMPERATURE,
        max_tokens=300,  # literal in router_agent.py:472, not a class constant
        disable_safety=True,
        response_mime_type="application/json",
        response_schema=RouterAgent.TRIAGE_RESPONSE_SCHEMA,
    )


async def one_call(adapter, model: str, prompt: str) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        response = await adapter.generate_content(request=build_request(model, prompt))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "latency": time.perf_counter() - started}
    latency = time.perf_counter() - started
    text = response.text or ""
    um = response.usage_metadata
    return {
        "latency": latency,
        "text": text,
        "triage": parse(text),
        "empty": not text.strip(),
        "prompt_tokens": (um.prompt_tokens if um else 0),
        "completion_tokens": (um.completion_tokens if um else 0),
    }


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #

def pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    -"


def report_leg(name: str, model: str, calls: List[dict]) -> dict:
    ok = [c for c in calls if "error" not in c]
    lat = sorted(c["latency"] for c in ok)
    parsed = sum(1 for c in ok if c["triage"] is not None)
    empties = sum(1 for c in ok if c["empty"])
    errors = len(calls) - len(ok)
    cost = calculate_cost(
        model=model,
        prompt_tokens=sum(c.get("prompt_tokens", 0) for c in ok),
        completion_tokens=sum(c.get("completion_tokens", 0) for c in ok),
    )
    print(f"\n  {name}  ({model})")
    if lat:
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))]
        print(f"    latency      p50={statistics.median(lat):5.2f}s  p95={p95:5.2f}s  "
              f"min={lat[0]:4.2f}  max={lat[-1]:4.2f}  stdev={statistics.pstdev(lat):4.2f}")
    print(f"    valid JSON   {pct(parsed, len(calls))}  ({parsed}/{len(calls)})")
    print(f"    EMPTY replies{pct(empties, len(calls))}  ({empties})"
          + ("   ← the documented Flash-Lite failure mode" if empties else ""))
    if errors:
        print(f"    errors       {errors}  e.g. {calls[[i for i,c in enumerate(calls) if 'error' in c][0]]['error'][:90]}")
    print(f"    cost         ${cost:.4f} for this run")
    return {"latencies": lat, "parsed": parsed, "empties": empties, "errors": errors, "cost": cost}


def main_report(rows: List[dict], repeats: int, models: Dict[str, str]) -> dict:
    print("\n" + "=" * 78)
    print("  STABILITY — same prompt, repeated: does the leg answer the same way?")
    print("=" * 78)
    self_consistent = {leg: defaultdict(int) for leg in models}
    counted = {leg: defaultdict(int) for leg in models}
    for row in rows:
        for leg in models:
            triages = [flatten(c["triage"]) for c in row["legs"][leg] if c.get("triage")]
            if len(triages) < 2:
                continue
            for f in FIELDS:
                counted[leg][f] += 1
                if len({canon(t.get(f)) for t in triages}) == 1:
                    self_consistent[leg][f] += 1
    header = "    field".ljust(30) + "".join(f"{leg:>16s}" for leg in models)
    print(header)
    for f in FIELDS:
        line = f"    {f}".ljust(30)
        for leg in models:
            line += f"{pct(self_consistent[leg][f], counted[leg][f]):>16s}"
        print(line + ("   ← selects Smart's tier" if f == "metadata.task_complexity" else ""))

    print("\n" + "=" * 78)
    print("  QUALITY — agreement, not correctness (no ground truth exists)")
    print("=" * 78)
    legs = list(models)
    agree = defaultdict(int)
    total = defaultdict(int)
    disagreements = []
    for row in rows:
        modals = {}
        for leg in legs:
            triages = [flatten(c["triage"]) for c in row["legs"][leg] if c.get("triage")]
            modals[leg] = {f: modal([t.get(f) for t in triages]) for f in FIELDS} if triages else None
        if not all(modals.values()):
            continue
        differing = {}
        for f in FIELDS:
            total[f] += 1
            a, b = modals[legs[0]][f], modals[legs[1]][f]
            if canon(a) == canon(b):
                agree[f] += 1
            else:
                differing[f] = {legs[0]: a, legs[1]: b}
        if differing:
            disagreements.append({
                "prompt_tail": row["prompt"][-600:],
                "production_model": row.get("prod_model"),
                "production_answer": flatten(parse(row.get("prod_response") or "")),
                "differing_fields": differing,
            })
    print(f"    cross-provider agreement ({legs[0]} vs {legs[1]}), modal answer per prompt:\n")
    for f in FIELDS:
        tag = ""
        if f in CONSEQUENTIAL:
            tag = "   ← consequential"
        elif f in FREE_TEXT:
            tag = "   (free text — phrasing differs, not necessarily wrong)"
        print(f"    {f}".ljust(30) + f"{pct(agree[f], total[f]):>16s}" + tag)
    return {"disagreements": disagreements}


async def main(limit: int, repeats: int, gemini_model: str, openai_model: str) -> None:
    print(f"Loading up to {limit} distinct real router prompts from BigQuery...")
    corpus = load_corpus(limit)
    print(f"  got {len(corpus)} prompts "
          f"({corpus[-1]['timestamp'][:10]} .. {corpus[0]['timestamp'][:10]})")

    oa = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
    # trust_env=False so a local proxy cannot advantage one leg over the other.
    oa.client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=300.0, max_retries=2,
                            http_client=httpx.AsyncClient(trust_env=False, timeout=300.0))
    ge = GeminiAdapter(api_key=os.environ["GEMINI_API_KEY"])

    models = {"openai": openai_model, "gemini": gemini_model}
    adapters = {"openai": oa, "gemini": ge}

    rows, all_calls = [], {leg: [] for leg in models}
    total_calls = len(corpus) * repeats * len(models)
    done = 0
    for item in corpus:
        prompt = item["request_text"]
        row = {"prompt": prompt, "prod_model": item.get("model"),
               "prod_response": item.get("response_text"), "legs": {leg: [] for leg in models}}
        for _ in range(repeats):
            # Interleaved: network drift hits both legs equally.
            for leg in models:
                call = await one_call(adapters[leg], models[leg], prompt)
                row["legs"][leg].append(call)
                all_calls[leg].append(call)
                done += 1
                if done % 20 == 0:
                    print(f"  ... {done}/{total_calls} calls", flush=True)
        rows.append(row)

    print("\n" + "=" * 78)
    print(f"  LATENCY & VALIDITY — {len(corpus)} prompts x {repeats} repeats, non-streaming")
    print("=" * 78)
    for leg in models:
        report_leg(leg, models[leg], all_calls[leg])

    extra = main_report(rows, repeats, models)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"router_ab_{stamp}.json"
    out.write_text(json.dumps({
        "models": models, "prompts": len(corpus), "repeats": repeats,
        "disagreements": extra["disagreements"],
    }, ensure_ascii=False, indent=1))
    print(f"\n  {len(extra['disagreements'])} prompts with at least one differing field")
    print(f"  full disagreement dump → {out}")
    print("  Read it. The percentages say where to look; only the dump says who was right.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=25, help="distinct real prompts to replay")
    ap.add_argument("--repeats", type=int, default=3, help="runs per prompt per leg (stability)")
    # Explicit versioned IDs only — an alias can be repointed under the measurement.
    ap.add_argument("--gemini-model", default="gemini-3.5-flash-lite")
    ap.add_argument("--openai-model", default="gpt-5.4-nano")
    args = ap.parse_args()
    asyncio.run(main(args.limit, args.repeats, args.gemini_model, args.openai_model))
