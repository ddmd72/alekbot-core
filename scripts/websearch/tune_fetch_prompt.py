#!/usr/bin/env python3
"""
fetch_url prompt tuning on gpt-5.4-nano (ECO)
=============================================
Finds a `_FALLBACK_FETCH_SYSTEM` wording that a small model executes correctly.

Background: the shipped prompt says "Return the complete page text without omissions"
while the per-call user message asks for specific extracted items. luna ignores the
contradiction; nano obeys it literally and returns navigation menus, button labels and
line-numbered page dumps — or nothing at all. Measured 2026-07-29.

The prompt must serve TWO shapes, because `_handle_fetch_url` builds
`user_content = f"{query}\\n\\n{url}" if query else url`:
  • WITH QUERY  — briefing/Round-3 style: "return only items from the last 24h, ..."
  • BARE URL    — user just handed over a link and said nothing.
A wording that only works for the first shape is not acceptable.

Scoring is deterministic — no LLM judge:
  urls          : sourced items (the briefing needs links)
  chrome        : page-furniture markers that must NOT appear
  chars         : payload size (it all flows into Smart on gpt-5.6-sol at $5/$30)
  empty         : silent failure

Usage:
    python scripts/websearch/tune_fetch_prompt.py
    python scripts/websearch/tune_fetch_prompt.py --prompts current,dual --sources 3
    python scripts/websearch/tune_fetch_prompt.py --model gpt-5.6-luna   # regression check
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

# See ab_nano_vs_luna.py — bypass the local MITM proxy picked up from macOS sysconf.
os.environ.setdefault("NO_PROXY", "*")

from dotenv import load_dotenv

load_dotenv()

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.billing import calculate_cost
from src.domain.llm import LLMRequest, Message, MessagePart

DEFAULT_MODEL = "gpt-5.4-nano"

# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #

CANDIDATES: Dict[str, str] = {
    # Shipped today. Baseline — "without omissions" is the line that backfires.
    "current": (
        "Fetch the provided URL and return its full content in detail. "
        "Return the complete page text without omissions. "
        "Slack mrkdwn only. No JSON. No code blocks."
    ),
    # Fixes the contradiction but leaves the bare-URL case undefined
    # ("the items the user asks for" — when the user asked for nothing).
    "extract_only": (
        "Fetch the provided URL and extract the items the user asks for. "
        "Report only editorial content — ignore navigation, menus, buttons, "
        "cookie notices and other page chrome. "
        "Slack mrkdwn only. No JSON. No code blocks."
    ),
    # Explicit about both shapes.
    "dual": (
        "Fetch the provided URL and report its substantive content.\n"
        "If the request states what to extract, return exactly that and nothing else.\n"
        "If it states nothing, report what the page holds: for a feed or index page, "
        "list its items; for a single article, give its content in full.\n"
        "Report editorial content only. Ignore navigation, menus, buttons, ads, "
        "cookie and subscription notices, related-article rails and other page furniture.\n"
        "Give the source URL for every item you list.\n"
        "Slack mrkdwn only. No JSON. No code blocks."
    ),
    # Same contract, compressed — tests whether the wording needs the length.
    "dual_terse": (
        "Fetch the provided URL. Return what the request asks for; if it asks for "
        "nothing specific, list the page's items (feed or index) or give the article's "
        "content in full. Editorial content only — skip navigation, menus, buttons, ads "
        "and cookie notices. Include each item's URL. "
        "Slack mrkdwn only. No JSON. No code blocks."
    ),
}

# --------------------------------------------------------------------------- #
# Fixtures: real briefing sources + one article page (Round-3 verification shape)
# --------------------------------------------------------------------------- #

NEWS_QUERY = (
    "Return only items published or materially updated in the last 24 hours. "
    "For each: headline, 1-2 line factual summary, article URL, timestamp. "
    "Skip events/agenda/what's-on listings. "
    "If the page does not load or parse, return nothing."
)

SOURCES: List[Dict[str, str]] = [
    # Failed outright on nano under the shipped prompt.
    {"url": "https://www.lavanguardia.com/", "kind": "index"},
    {"url": "https://www.rtve.es/noticias/", "kind": "index"},
    {"url": "https://www.anthropic.com/news", "kind": "index"},
    # Returned page chrome instead of items.
    {"url": "https://www.aljazeera.com/news/", "kind": "index"},
    {"url": "https://www.nature.com/news", "kind": "index"},
    {"url": "https://www.valencia.es/cas/actualidad", "kind": "index"},
    # Article page — fetch_url is also used for Round-3 verification.
    {"url": "https://www.theguardian.com/world", "kind": "index"},
]

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")
_CHROME_RE = re.compile(
    r"(?:^L\d+:)|Skip to |\[Button|Sign up|Subscribe|Suscr[íi]b|Iniciar sesi[óo]n"
    r"|Accept (?:all|cookies)|Cookie|Newsletter|Men[úu]\b|Navigation",
    re.I | re.M,
)


def score(text: str) -> Dict[str, Any]:
    return {
        "urls": len(_URL_RE.findall(text or "")),
        "chrome": len(_CHROME_RE.findall(text or "")),
        "chars": len(text or ""),
        "empty": not (text or "").strip(),
    }


async def run_one(adapter: OpenAIAdapter, model: str, system: str, user: str,
                  sem: asyncio.Semaphore) -> Dict[str, Any]:
    async with sem:
        t0 = time.perf_counter()
        try:
            resp = await adapter.generate_content(LLMRequest(
                model_name=model,
                system_instruction=system,
                messages=[Message(role="user", parts=[MessagePart(text=user)])],
                use_grounding=True,
            ))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "latency_s": time.perf_counter() - t0,
                    "urls": 0, "chrome": 0, "chars": 0, "empty": True, "cost": 0.0, "text": ""}
        latency = time.perf_counter() - t0
        u = resp.usage_metadata
        out = {"text": resp.text or "", "latency_s": latency,
               "cost": calculate_cost(
                   model,
                   getattr(u, "prompt_tokens", 0) if u else 0,
                   getattr(u, "completion_tokens", 0) if u else 0,
                   cache_read_tokens=getattr(u, "cache_read_tokens", 0) if u else 0,
               )}
        out.update(score(out["text"]))
        return out


async def main() -> None:
    ap = argparse.ArgumentParser(description="Tune the fetch_url system prompt on a small model")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--prompts", default=",".join(CANDIDATES),
                    help="comma-separated candidate names")
    ap.add_argument("--sources", type=int, default=0, help="cap sources (0 = all)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    names = [n.strip() for n in args.prompts.split(",") if n.strip()]
    unknown = [n for n in names if n not in CANDIDATES]
    if unknown:
        raise SystemExit(f"unknown candidates: {unknown}. Known: {list(CANDIDATES)}")
    sources = SOURCES[: args.sources] if args.sources else SOURCES

    adapter = OpenAIAdapter(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(args.concurrency)

    # mode → user_content shape, mirroring _handle_fetch_url exactly.
    modes = {
        "with_query": lambda url: f"{NEWS_QUERY}\n\n{url}",
        "bare_url": lambda url: url,
    }

    jobs, keys = [], []
    for name in names:
        for src in sources:
            for mode, build in modes.items():
                keys.append((name, src["url"], mode))
                jobs.append(run_one(adapter, args.model, CANDIDATES[name],
                                    build(src["url"]), sem))
    print(f"{len(jobs)} calls on {args.model}: "
          f"{len(names)} prompts x {len(sources)} sources x {len(modes)} modes\n")
    results = dict(zip(keys, await asyncio.gather(*jobs)))

    for mode in modes:
        print(f"\n{'=' * 104}\nMODE: {mode}\n{'=' * 104}")
        header = f"{'source':<30}" + "".join(f"{n:>18}" for n in names)
        print(header)
        print(f"{'':<30}" + "".join(f"{'urls/chrome/chars':>18}" for _ in names))
        for src in sources:
            row = f"{src['url'].split('//')[-1][:29]:<30}"
            for name in names:
                r = results[(name, src["url"], mode)]
                cell = "ERROR" if "error" in r else (
                    f"{r['urls']:>3}u {r['chrome']:>2}ch {r['chars']:>6}c"
                    + ("!" if r["empty"] else " ")
                )
                row += f"{cell:>18}"
            print(row)
        print()
        for name in names:
            rs = [results[(name, s["url"], mode)] for s in sources]
            ok = [r for r in rs if "error" not in r]
            n = len(ok) or 1
            print(f"  {name:<13} avg_urls={sum(r['urls'] for r in ok) / n:5.1f}"
                  f"  chrome_hits={sum(r['chrome'] for r in ok):3d}"
                  f"  avg_chars={sum(r['chars'] for r in ok) / n:7.0f}"
                  f"  empty={sum(1 for r in ok if r['empty'])}/{len(ok)}"
                  f"  avg_lat={sum(r['latency_s'] for r in ok) / n:5.1f}s"
                  f"  cost=${sum(r['cost'] for r in ok):.4f}")

    out_dir = Path(__file__).parent.parent / "memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = out_dir / f"fetch_prompt_tuning_{args.model}.json"
    dump.write_text(json.dumps(
        {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in results.items()},
        indent=2, ensure_ascii=False))
    print(f"\nVerbatim outputs → {dump}")


if __name__ == "__main__":
    asyncio.run(main())
