#!/usr/bin/env python3
"""
MapsSearchAgent model shootout — cheap Gemini vs cheap OpenAI vs the current default
====================================================================================
maps_search is provider-agnostic (`allowed_providers: ["openai", "gemini", "claude"]`,
`required_capabilities: ["native_tools"]`), so the model is a free choice. This runs the
REAL MapsSearchAgent against the LIVE Google Maps AI Grounding MCP server — the whole
multi-turn loop, real tool calls, real arguments. Only `model_name` and the provider
adapter differ between candidates; the Firestore-assembled system prompt is replayed
verbatim from BigQuery, so every candidate is judged on the same instruction.

VERDICT 2026-07-29: keep gpt-5.6-luna. Recorded in
src/domain/user.py::_DEFAULT_AGENT_TIERS["maps_search"] — read that before re-running.

READ THIS BEFORE TRUSTING THE TABLE — the metrics here are decision *aids*, not the
decision. Two of them misled badly during the original run:

  • tool call count is NOT a score. It only reads as one on queries the maps tools can
    actually serve. `maps_query` is auto-fanned-out from every `search_web`, so most
    recorded traffic is ticket/news verification that no place-search tool can answer;
    there, refusing is correct and calling tools is waste. Use --fresh (or --geo-only).
  • "used tools" does not mean "grounded". The decisive failure found here was a model
    making a few real calls and then inventing the details under genuine place links:
    review counts as "27,000+" where Maps returns 57,563, ratings off by 0.1-0.3, wrong
    street numbers. Cheap models look fast and cheap precisely because they skip work —
    3 tool calls where luna spends 13.

So: the table narrows the field, then you MUST read the answers. Exact review counts,
whether a "ranked by" instruction was honoured, whether every entry is really the kind of
thing asked for, and whether the model states what the tools cannot answer (no showtimes,
no route alternatives) rather than filling the gap. The verbatim outputs are dumped for
exactly that.

NOTE: real LLM calls + real MCP calls per candidate. Run deliberately.

Usage:
    python scripts/websearch/ab_maps_models.py --fresh                      # the real suite
    python scripts/websearch/ab_maps_models.py --fresh --models luna,flash-3.5l
    python scripts/websearch/ab_maps_models.py --geo-only                   # recorded geo only
    python scripts/websearch/ab_maps_models.py --limit 3                    # raw recorded (noisy)
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
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# See ab_nano_vs_luna.py — bypass the MITM proxy inherited from macOS system settings.
os.environ.setdefault("NO_PROXY", "*")

from dotenv import load_dotenv

load_dotenv()

from src.adapters.gemini_adapter import GeminiAdapter
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.adapters.mcp.mcp_client import MCPClient
from src.adapters.mcp.mcp_maps_adapter import MCPMapsAdapter
from src.adapters.openai_adapter import OpenAIAdapter
from src.agents.maps_search_agent import MapsSearchAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage
from src.domain.user import PerformanceTier
from src.infrastructure.agent_config import MAPS_SEARCH as MAPS_CFG
from src.ports.llm_port import AgentExecutionContext, ProviderCapabilities

MAPS_MCP_URL = "https://mapstools.googleapis.com/mcp"

# name → (provider, model). Cheap candidates plus the current default as the baseline.
# CAUTION: "flash-lite" and "flash-3.5l" are THE SAME MODEL. The Gemini ECO alias
# `gemini-flash-lite-latest` resolves to `gemini-3.5-flash-lite` as of 2026-07-29
# (`make check-pricing` prints the live resolution). They are kept as separate entries
# only because running an alias and its target side by side is a cheap way to see this
# benchmark's run-to-run variance — which is LARGE here. Do not read a difference between
# those two rows as a difference between models.
CANDIDATES: Dict[str, Tuple[str, str]] = {
    "flash-lite": ("gemini", "gemini-flash-lite-latest"),   # ECO alias → gemini-3.5-flash-lite
    "flash-3":    ("gemini", "gemini-3-flash-preview"),     # $0.50/$3.00 — 3-flash-preview-12-2025
    "flash-3.5l": ("gemini", "gemini-3.5-flash-lite"),      # $0.30/$2.50 — same as flash-lite
    "flash-3.6":  ("gemini", "gemini-3.6-flash"),           # $1.50/$7.50 — newest, DEARER than luna
    "nano":       ("openai", "gpt-5.4-nano"),               # $0.20/$1.25 — OpenAI ECO
    "mini":       ("openai", "gpt-5.4-mini"),               # $0.75/$4.50 — pre-2026-07-13 default
    "luna":       ("openai", "gpt-5.6-luna"),               # billing.py $1.00/$6.00 — CURRENT
}

# NOTE ON COST FIGURES: they come from src/domain/billing.py, which `make check-pricing`
# flags as 2x HIGH for gpt-5.6-luna/terra and 1.5x high for claude-sonnet-5 against live
# OpenRouter prices (2026-07-29). Unresolved — OpenRouter is a reseller and the OpenAI
# costs endpoint needs the api.usage.read scope this key lacks. So treat cost RATIOS
# between an OpenAI and a Gemini model here as provisional; latency and quality are
# unaffected.

# Models absent from src/domain/billing.py. Without these, calculate_cost() silently
# returns 0.0 and the cost column lies. Verified against live OpenRouter prices
# 2026-07-29. Injected at runtime — production pricing stays untouched until a model is
# actually adopted, at which point it needs a real entry in billing.py.
EXPERIMENTAL_PRICES: Dict[str, Dict[str, float]] = {
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.25},
    "gemini-3.6-flash":      {"input": 1.50, "output": 7.50, "cache_read": 0.25},
}


def install_experimental_prices() -> None:
    from src.domain.billing import _PRICING_PER_MILLION_TOKENS as table

    added = [m for m in EXPERIMENTAL_PRICES if m not in table]
    table.update({m: p for m, p in EXPERIMENTAL_PRICES.items() if m not in table})
    if added:
        print(f"ℹ️  injected experimental pricing (absent from billing.py): {', '.join(added)}")


class _RecordedPromptBuilder:
    """Serves the recorded Firestore-assembled prompt.

    Faithful without booting PromptBuilder + Firestore, and guarantees every candidate is
    judged on the identical system instruction.
    """

    def __init__(self, prompt: str) -> None:
        self._prompt = prompt

    async def build_for_agent(self, *args: Any, **kwargs: Any) -> str:
        return self._prompt


# Queries whose answer genuinely lives in the maps tools (place discovery, routes,
# weather) — hand-picked from a month of recordings.
#
# WHY HAND-PICKED: `maps_query` is auto-fanned-out from EVERY `search_web`, and most of
# what arrives cannot be served by search_places/compute_routes/lookup_weather at all —
# "verify the ticket price and cancellation status from the organiser's page", "run a
# broad current-news scan". On those, refusing IS the correct answer, so scoring
# tool usage there rewards the wrong behaviour (measured 2026-07-29: luna spent 3 tool
# calls and 9.4s on one such query only to conclude "the results only confirmed the
# venues"; the cheap models declined for a quarter of the price). Only 4 genuinely
# geographic queries exist in ~430 recorded calls — that scarcity is itself a finding.
GEO_SELECTORS: List[str] = [
    "Find current jewellers and gold jewellery repair services",   # place discovery
    "worth seeing in Serra",                                       # places + route + dinner
    "retrieve current live weather and today's forecast",           # weather lookup
    "verify current weather in Puçol and Valencia",                 # weather lookup
]


# Purpose-built geo suite (owner-specified 2026-07-29). Written rather than replayed, to
# remove the two confounds that made the recorded sets unreadable:
#   - fan-out noise: replayed queries were mostly ticket/news verification that the maps
#     tools cannot serve, so refusing was correct and tool-count scored it as failure;
#   - stale dates: replaying a query dated "24 July" while the agent stamps today's date
#     rewarded the model that failed to notice (it served today's weather as the 24th's).
# MapsSearchAgent prepends current_date_time to the user message itself, so these are
# always evaluated against today.
#
# Two of the five deliberately sit at a capability boundary — the adapter exposes place
# search, weather and "distance + duration" routing, with no showtimes and no documented
# route alternatives. Whether a model states that limit or invents past it is the point.
FRESH_GEO_QUERIES: List[str] = [
    # 1. place discovery + driving radius + ranking by rating
    "Find Carrefour stores within a 15 km drive of Puçol, Valencia, Spain. "
    "Sort them by customer rating (best first) and give for each: name, full address, "
    "rating and number of reviews, opening hours, and driving distance and time from Puçol.",
    # 2. place discovery + NON-CAR routing + opening hours
    "List police stations that are sensible to reach by bicycle from Av. Font de Mora 3, "
    "Puçol, Valencia, Spain. For each: name, full address, opening hours, phone, and the "
    "cycling distance and time from that address. Exclude any that are impractical by bike.",
    # 3. place discovery + ranking, needs 10 distinct results
    "Top 10 museums in Valencia, Spain, ranked by rating. For each give name, address, "
    "rating with review count, and opening hours for today.",
    # 4. BOUNDARY: cinema listings are place data, today's VOSE showtimes are not
    "List cinemas in Valencia, Spain that are showing films in VOSE (original version with "
    "Spanish subtitles) today. For each: cinema name, address, and which VOSE films are "
    "screening today with their times.",
    # 5. BOUNDARY: long international route with THREE alternatives
    "Driving distance from Puçol, Valencia, Spain to Angers, France. Give three "
    "alternative routes with details for each: total distance, driving time, the main "
    "roads and countries crossed, and any tolls.",
]


def load_fresh_geo_queries(project: str, dataset: str) -> List[Dict[str, str]]:
    """Pair the owner-specified geo queries with the real assembled maps system prompt.

    The system prompt is taken from the newest recording — it is the genuine Firestore
    assembly and is stable across calls, so every candidate is judged on the prompt
    production actually uses.
    """
    rows = _query_turn1(project, dataset, tools_only=True, limit=1)
    parsed = _parse_row(rows[0]) if rows else None
    if not parsed:
        raise SystemExit("no recorded maps_search call to source the system prompt from")
    return [{"query": q, "system": parsed["system"]} for q in FRESH_GEO_QUERIES]


def load_geo_queries(project: str, dataset: str) -> List[Dict[str, str]]:
    """Recorded calls whose task the maps tools can actually perform."""
    rows = _query_turn1(project, dataset, tools_only=True, limit=160)
    out: List[Dict[str, str]] = []
    for needle in GEO_SELECTORS:
        for r in rows:
            parsed = _parse_row(r)
            if parsed and needle.lower() in parsed["query"].lower():
                out.append(parsed)
                break
    return out


def _parse_row(row: Dict[str, Any]) -> Dict[str, str] | None:
    sys_m = re.search(r"=== SYSTEM ===\n(.*?)\n\n=== MESSAGES ===", row["request_text"], re.S)
    usr_m = re.search(r"^user: (.*)$", row["request_text"], re.S | re.M)
    if not (sys_m and usr_m):
        return None
    query = re.sub(r"^current_date_time:.*?\n\n", "", usr_m.group(1).strip(), flags=re.S)
    return {"query": query, "system": sys_m.group(1).strip()}


def _query_turn1(project: str, dataset: str, tools_only: bool, limit: int) -> List[Dict[str, Any]]:
    tool_filter = ('AND tool_calls IS NOT NULL AND tool_calls NOT IN ("", "[]")'
                   if tools_only else "")
    sql = f"""
    SELECT request_text
    FROM `{project}.{dataset}.prompt_content`
    WHERE agent_type = "maps_search" AND turn = 1 {tool_filter}
    ORDER BY timestamp DESC
    LIMIT {limit}
    """
    return json.loads(subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", f"--max_rows={limit}", sql],
        capture_output=True, text=True, check=True,
    ).stdout)


def load_recorded_queries(project: str, dataset: str, limit: int) -> List[Dict[str, str]]:
    """Newest distinct turn-1 maps_search calls that ACTUALLY exercised the maps tools.

    The `tool_calls IS NOT NULL` filter is what makes this benchmark mean anything.
    maps_query is auto-fanned-out from every `search_web`, so most recorded calls are news
    queries with no geographic content — production answers them with a 43-char "no map
    results" and zero tool calls (286 of 434 turn-1 rows). Benchmarking on those would
    compare two models at declining to answer.
    """
    sql = f"""
    SELECT request_text
    FROM `{project}.{dataset}.prompt_content`
    WHERE agent_type = "maps_search" AND turn = 1
      AND tool_calls IS NOT NULL AND tool_calls NOT IN ("", "[]")
    ORDER BY timestamp DESC
    LIMIT 120
    """
    rows = json.loads(subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=120", sql],
        capture_output=True, text=True, check=True,
    ).stdout)

    out, seen = [], set()
    for r in rows:
        sys_m = re.search(r"=== SYSTEM ===\n(.*?)\n\n=== MESSAGES ===", r["request_text"], re.S)
        usr_m = re.search(r"^user: (.*)$", r["request_text"], re.S | re.M)
        if not (sys_m and usr_m):
            continue
        # The agent prepends current_date_time to the query; strip it so the replay
        # re-adds today's.
        query = re.sub(r"^current_date_time:.*?\n\n", "", usr_m.group(1).strip(), flags=re.S)
        key = " ".join(query.split())[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append({"query": query, "system": sys_m.group(1).strip()})
        if len(out) >= limit:
            break
    return out


def build_agent(name: str, case: Dict[str, str], maps_port: MCPMapsAdapter) -> MapsSearchAgent:
    provider_name, model = CANDIDATES[name]
    if provider_name == "gemini":
        provider = GeminiAdapter(api_key=os.environ["GEMINI_API_KEY"])
        tier = PerformanceTier.ECO
    else:
        provider = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
        tier = PerformanceTier.ECO if "nano" in model else PerformanceTier.BALANCED

    ctx = AgentExecutionContext(
        agent_type="maps_search",
        provider=provider,
        model_name=model,
        tier=tier,
        capabilities=ProviderCapabilities(),
        provider_name=provider_name,
        resilience_port=InMemoryProviderResilience(),
    )
    return MapsSearchAgent(
        config=AgentConfig(
            agent_id=f"maps_ab_{name}",
            agent_type="maps_search",
            timeout_ms=MAPS_CFG.timeout_ms,
            capabilities=["location_search", "place_search", "routing", "weather"],
        ),
        execution_context=ctx,
        maps_port=maps_port,
        prompt_builder=_RecordedPromptBuilder(case["system"]),
    )


async def run_case(name: str, case: Dict[str, str], maps_port: MCPMapsAdapter,
                   sem: asyncio.Semaphore) -> Dict[str, Any]:
    """One candidate on one query, through the real agent and real MCP."""
    async with sem:
        agent = build_agent(name, case, maps_port)
        tool_calls, tool_errors = 0, 0

        # Count tool traffic without touching the agent: wrap the port method it calls.
        original = agent._maps_port.call_tool

        async def counting_call_tool(tool: str, arguments: dict) -> dict:
            nonlocal tool_calls, tool_errors
            tool_calls += 1
            try:
                return await original(tool, arguments)
            except Exception:
                tool_errors += 1
                raise

        agent._maps_port = _PortProxy(agent._maps_port, counting_call_tool)

        message = AgentMessage.create(
            sender="ab", recipient=agent.agent_id, intent=AgentIntent.QUERY,
            payload={"query": case["query"]}, context={},
        )
        t0 = time.perf_counter()
        try:
            response = await agent.process(message)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "latency_s": time.perf_counter() - t0,
                    "cost": 0.0, "tool_calls": tool_calls, "tool_errors": tool_errors,
                    "chars": 0, "text": "", "status": "exception"}
        latency = time.perf_counter() - t0

        text = ""
        if isinstance(response.result, dict):
            text = response.result.get("text", "") or ""
        elif isinstance(response.result, str):
            text = response.result

        _, model = CANDIDATES[name]
        # The ledger BaseAgent filled for this execution is the honest token source.
        return {
            "status": str(response.status),
            "text": text,
            "chars": len(text),
            "latency_s": latency,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "cost": _cost_from_ledger(model),
        }


class _PortProxy:
    """Delegates everything to the real port, but routes call_tool through a counter."""

    def __init__(self, inner: MCPMapsAdapter, call_tool) -> None:
        self._inner = inner
        self.call_tool = call_tool

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


_LAST_LEDGER: Dict[str, float] = {}


def _cost_from_ledger(model: str) -> float:
    """Read the per-execution token ledger BaseAgent just flushed.

    BaseAgent scopes it to the execution via a ContextVar and resets it on exit, so it
    cannot be read after process() returns. We therefore recompute from the value the
    agent logged — see _install_ledger_probe.
    """
    return _LAST_LEDGER.pop(model, 0.0)


def _install_ledger_probe() -> None:
    """Capture each execution's token ledger before its scope closes.

    Wraps BaseAgent._flush_billing (a no-op without a quota service) purely to read the
    ledger while it is still current. Keeps the harness honest about cost without
    duplicating token bookkeeping.
    """
    from src.agents import base_agent as ba

    original = ba.BaseAgent._flush_billing

    async def probing_flush(self) -> None:  # type: ignore[no-untyped-def]
        ledger = ba._EXECUTION_LEDGER.get()
        if ledger is not None:
            model = getattr(self, "model_name", "") or ""
            # Grouped by the agent's configured model; priced per model that ran.
            _LAST_LEDGER[model] = _LAST_LEDGER.get(model, 0.0) + ledger.cost()
        await original(self)

    ba.BaseAgent._flush_billing = probing_flush


async def main() -> None:
    ap = argparse.ArgumentParser(description="MapsSearchAgent model shootout")
    ap.add_argument("--models", default=",".join(CANDIDATES))
    ap.add_argument("--limit", type=int, default=5, help="distinct recorded queries")
    ap.add_argument("--fresh", action="store_true",
                    help="use the purpose-built geo suite (FRESH_GEO_QUERIES) — recommended")
    ap.add_argument("--geo-only", action="store_true",
                    help="use only queries the maps tools can actually serve (see GEO_SELECTORS)")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="keep low — the MCP server is shared and rate-limited")
    args = ap.parse_args()

    names = [n.strip() for n in args.models.split(",") if n.strip()]
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown:
        raise SystemExit(f"unknown models {unknown}; known: {list(CANDIDATES)}")

    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    dataset = os.getenv("BIGQUERY_PROMPT_DATASET", "alek_observability_dev")
    if args.fresh:
        cases = load_fresh_geo_queries(project, dataset)
    elif args.geo_only:
        cases = load_geo_queries(project, dataset)
    else:
        cases = load_recorded_queries(project, dataset, args.limit)
    if not cases:
        print("No recorded maps_search turn-1 calls found")
        return

    install_experimental_prices()
    _install_ledger_probe()
    maps_port = MCPMapsAdapter(MCPClient(
        base_url=MAPS_MCP_URL,
        api_key=os.environ.get("GOOGLE_SEARCH_API_KEY", ""),
    ))

    print(f"{len(cases)} recorded queries x {len(names)} models "
          f"(thinking={MAPS_CFG.thinking!r}, real MCP calls)\n")
    for i, c in enumerate(cases, 1):
        print(f"  {i}. {' '.join(c['query'].split())[:110]}")

    sem = asyncio.Semaphore(args.concurrency)
    results: Dict[str, List[Dict[str, Any]]] = {}
    for name in names:
        # Sequential per model so latency is not distorted by cross-model contention.
        results[name] = [await run_case(name, c, maps_port, sem) for c in cases]

    print(f"\n{'=' * 104}")
    print(f"{'model':<12} {'$/query':>9} {'latency':>9} {'tools':>7} {'errors':>7} "
          f"{'no-tools':>9} {'avg chars':>10}")
    print(f"{'=' * 104}")
    for name in names:
        rs = results[name]
        ok = [r for r in rs if "error" not in r]
        k = len(ok) or 1
        # "Gave up" is the real failure, and it is NOT an empty string: MapsSearchAgent
        # returns the "No map results found" sentinel, so a text-emptiness check scores a
        # silent failure as a success. Zero tool calls is the honest signal — the agent
        # answered without ever consulting Google Maps.
        empty = sum(1 for r in ok if r["tool_calls"] == 0)
        print(f"{name:<12} "
              f"{sum(r['cost'] for r in ok) / k:>9.5f} "
              f"{sum(r['latency_s'] for r in ok) / k:>8.1f}s "
              f"{sum(r['tool_calls'] for r in ok) / k:>7.1f} "
              f"{sum(r['tool_errors'] for r in ok):>7d} "
              f"{empty:>5d}/{len(ok):<3d} "
              f"{sum(r['chars'] for r in ok) / k:>10.0f}"
              + (f"   [{len(rs) - len(ok)} exceptions]" if len(rs) - len(ok) else ""))

    print(f"\n{'per-query latency (s)':<24}" + "".join(f"{n:>12}" for n in names))
    for i in range(len(cases)):
        row = f"  q{i + 1:<21}"
        for name in names:
            r = results[name][i]
            cell = "ERR" if "error" in r else f"{r['latency_s']:.1f}"
            row += f"{cell:>12}"
        print(row)

    out = Path(__file__).parent.parent / "memory" / "maps_models_ab.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"queries": [c["query"] for c in cases], "results": results},
        indent=2, ensure_ascii=False))
    print(f"\nFull answers per model → {out}")


if __name__ == "__main__":
    asyncio.run(main())
