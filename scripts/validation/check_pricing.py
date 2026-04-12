"""
Model pricing audit: fetch live prices from providers, compare with billing.py.

Sources:
  - OpenRouter  GET https://openrouter.ai/api/v1/models  (no auth, all providers)
  - OpenAI      GET https://api.openai.com/v1/models     (lists available model IDs)
  - Gemini      genai.list_models()                      (lists available model IDs)

Output: scripts/memory/pricing_report.md  (gitignored)

Usage:
  python scripts/validation/check_pricing.py [--out PATH]
  make check-pricing
"""

import asyncio
import os
import sys
import argparse
import re
import textwrap
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_openrouter_prices() -> dict[str, dict]:
    """Returns {openrouter_id: {input, output}} prices per million tokens."""
    import urllib.request
    import json
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as r:
        data = json.loads(r.read())
    result = {}
    for m in data.get("data", []):
        p = m.get("pricing", {})
        try:
            result[m["id"]] = {
                "input":  round(float(p.get("prompt", 0)) * 1_000_000, 4),
                "output": round(float(p.get("completion", 0)) * 1_000_000, 4),
            }
        except (ValueError, TypeError):
            pass
    return result


async def _fetch_openai_model_ids() -> list[str]:
    """Returns list of model IDs available via OpenAI API."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return []
    import openai
    client = openai.AsyncOpenAI(api_key=api_key)
    models = await client.models.list()
    return sorted(m.id for m in models.data)


async def _resolve_openai_aliases(aliases: list[str]) -> dict[str, str]:
    """Resolve OpenAI alias IDs to their current versioned model via a minimal completion call.
    Returns {alias: resolved_model_id}.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {}
    import openai
    client = openai.AsyncOpenAI(api_key=api_key)
    result = {}
    for alias in aliases:
        try:
            r = await client.chat.completions.create(
                model=alias,
                messages=[{"role": "user", "content": "hi"}],
                max_completion_tokens=5,
            )
            result[alias] = r.model
        except Exception as e:
            result[alias] = f"error: {e}"
    return result


def _fetch_gemini_model_ids() -> list[str]:
    """Returns list of Gemini model names (models/...) from google-genai SDK."""
    try:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return []
        client = genai.Client(api_key=api_key)
        return sorted(m.name for m in client.models.list())
    except Exception as e:
        print(f"  [warn] Gemini list_models failed: {e}", file=sys.stderr)
        return []


def _resolve_gemini_aliases(aliases: list[str]) -> dict[str, str]:
    """Resolve Gemini alias IDs to their current versioned model via a minimal generate call.
    Returns {alias: resolved_model_version} for each alias that resolves successfully.
    """
    try:
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            return {}
        client = genai.Client(api_key=api_key)
        result = {}
        for alias in aliases:
            try:
                r = client.models.generate_content(model=alias, contents="hi")
                result[alias] = getattr(r, "model_version", None) or "?"
            except Exception as e:
                result[alias] = f"error: {e}"
        return result
    except Exception as e:
        print(f"  [warn] Gemini alias resolution failed: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# billing.py reader
# ---------------------------------------------------------------------------

def _load_billing_entries() -> dict[str, dict]:
    """Import _PRICING_PER_MILLION_TOKENS directly from the domain module."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from src.domain.billing import _PRICING_PER_MILLION_TOKENS  # type: ignore
    return dict(_PRICING_PER_MILLION_TOKENS)


# ---------------------------------------------------------------------------
# Matching: billing.py key → OpenRouter ID
# ---------------------------------------------------------------------------

# Explicit overrides for IDs that don't auto-resolve.
# Expected cache multipliers per provider (source: official pricing pages).
# OpenRouter doesn't expose cache pricing, so we validate against known values.
_EXPECTED_CACHE: dict[str, dict[str, float]] = {
    "claude-":         {"cache_read": 0.10, "cache_write": 1.25},
    "gemini-":         {"cache_read": 0.25},
    "models/gemini-":  {"cache_read": 0.25},
    "deep-research-":  {"cache_read": 0.25},
    "gpt-":            {"cache_read": 0.10},
    "o3-":             {"cache_read": 0.10},
    "o4-":             {"cache_read": 0.10},
}


def _get_expected_cache(key: str) -> dict[str, float] | None:
    """Return expected cache multipliers for a billing key, or None if no cache expected."""
    for prefix, expected in _EXPECTED_CACHE.items():
        if key.startswith(prefix):
            return expected
    return None


_BILLING_TO_OR: dict[str, str] = {
    # Gemini aliases → resolved via generate call (model_version field)
    "gemini-flash-lite-latest":          "google/gemini-2.5-flash-lite",
    "gemini-flash-latest":               "google/gemini-3-flash-preview",
    "gemini-pro-latest":                 "google/gemini-3.1-pro-preview",
    "gemini-3-flash-preview":            "google/gemini-3-flash-preview",
    "models/gemini-3-pro-preview":       "google/gemini-3.1-pro-preview",
    "deep-research-pro-preview-12-2025": "google/gemini-2.5-pro",  # approx
    # OpenAI deep research (versioned → unversioned alias on OpenRouter)
    "o3-deep-research-2025-06-26":       "openai/o3-deep-research",
    "o4-mini-deep-research-2025-06-26":  "openai/o4-mini-deep-research",
    # Grok (not on OpenRouter, skip)
    "grok-4-1-fast-non-reasoning":       "",
    "grok-4-1-fast-reasoning":           "",
}


def _billing_key_to_or(key: str) -> str:
    """Best-effort mapping from billing.py key to OpenRouter model ID."""
    if key in _BILLING_TO_OR:
        return _BILLING_TO_OR[key]
    # Claude: claude-sonnet-4-6 → anthropic/claude-sonnet-4.6
    #         claude-haiku-4-5-20251001 → try anthropic/claude-haiku-4.5
    if key.startswith("claude-"):
        # strip trailing date suffix (-YYYYMMDD or -YYYYMMDD)
        base = re.sub(r"-\d{8}$", "", key)
        # last two hyphen-separated segments are version: X-Y → X.Y
        parts = base.split("-")
        # find the version part (digits.digits pattern)
        # e.g. claude-haiku-4-5 → claude-haiku, 4.5
        for i in range(len(parts) - 1, 0, -1):
            if parts[i].isdigit() and i > 0 and parts[i-1].isdigit():
                version = f"{parts[i-1]}.{parts[i]}"
                name = "-".join(parts[:i-1])
                return f"anthropic/{name}-{version}"
        return f"anthropic/{base}"
    # OpenAI: gpt-5.4-nano → openai/gpt-5.4-nano  (dots preserved)
    if key.startswith("gpt-") or key.startswith("o3-") or key.startswith("o4-"):
        return f"openai/{key}"
    return ""


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

PROVIDERS = {
    "anthropic": "Claude (Anthropic)",
    "google":    "Gemini (Google)",
    "openai":    "OpenAI",
    "x-ai":      "Grok (xAI)",
}

# Which OpenRouter prefixes to include in the live table
_OR_PREFIXES = tuple(PROVIDERS.keys())


def _build_report(
    or_prices: dict[str, dict],
    openai_ids: list[str],
    gemini_ids: list[str],
    billing: dict[str, dict],
    gemini_aliases: dict[str, str] | None = None,
    openai_aliases: dict[str, str] | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines.append(f"# Model Pricing Report — {now}\n")
    lines.append("Generated by `make check-pricing`.\n")
    lines.append("> Prices per million tokens (USD). Source: OpenRouter API.\n")

    # -----------------------------------------------------------------------
    # Section 1: Live prices by provider
    # -----------------------------------------------------------------------
    for prefix, label in PROVIDERS.items():
        rows = [
            (mid, p) for mid, p in sorted(or_prices.items())
            if mid.startswith(f"{prefix}/")
        ]
        if not rows:
            continue
        lines.append(f"\n## {label}\n")
        lines.append(f"| Model ID | Input $/M | Output $/M |")
        lines.append(f"|----------|----------:|----------:|")
        for mid, p in rows:
            lines.append(f"| `{mid}` | {p['input']:.3f} | {p['output']:.3f} |")

    # -----------------------------------------------------------------------
    # Section 2: Gemini alias resolution
    # -----------------------------------------------------------------------
    if gemini_aliases:
        lines.append("\n## Gemini — Alias resolution (live generate call)\n")
        lines.append("| Alias | Resolves to | OR input $/M | OR output $/M |")
        lines.append("|-------|-------------|-------------:|--------------:|")
        for alias, resolved in sorted(gemini_aliases.items()):
            # strip models/ prefix for OR lookup
            stripped = resolved.lstrip("models/")
            or_id = f"google/{stripped}"
            p = or_prices.get(or_id, {})
            in_s  = f"{p['input']:.3f}"  if p else "—"
            out_s = f"{p['output']:.3f}" if p else "—"
            lines.append(f"| `{alias}` | `{resolved}` | {in_s} | {out_s} |")

    # -----------------------------------------------------------------------
    # Section 3: OpenAI alias resolution
    # -----------------------------------------------------------------------
    if openai_aliases:
        lines.append("\n## OpenAI — Alias resolution (live completion call)\n")
        lines.append("> Note: billing uses the alias we call with, not the versioned ID — no billing impact.")
        lines.append("")
        lines.append("| Alias | Resolves to |")
        lines.append("|-------|-------------|")
        for alias, resolved in sorted(openai_aliases.items()):
            lines.append(f"| `{alias}` | `{resolved}` |")

    # -----------------------------------------------------------------------
    # Section 4: OpenAI available model IDs (from API)
    # -----------------------------------------------------------------------
    if openai_ids:
        gpt5 = [m for m in openai_ids if m.startswith("gpt-5")]
        o_series = [m for m in openai_ids if re.match(r"^o\d", m)]
        lines.append("\n## OpenAI — Available model IDs (from API)\n")
        if gpt5:
            lines.append("**GPT-5 family:**")
            lines.append("```")
            lines.extend(gpt5)
            lines.append("```")
        if o_series:
            lines.append("\n**o-series:**")
            lines.append("```")
            lines.extend(o_series)
            lines.append("```")

    # -----------------------------------------------------------------------
    # Section 3: Gemini available model IDs (from API)
    # -----------------------------------------------------------------------
    if gemini_ids:
        lines.append("\n## Gemini — Available model IDs (from API)\n")
        lines.append("```")
        lines.extend(gemini_ids)
        lines.append("```")

    # -----------------------------------------------------------------------
    # Section 4: billing.py audit
    # -----------------------------------------------------------------------
    lines.append("\n---\n")
    lines.append("## billing.py audit\n")
    lines.append("Compares every entry in `src/domain/billing.py` against live OpenRouter prices.\n")
    lines.append("| billing.py key | Billed input | Billed output | OR input | OR output | Status |")
    lines.append("|----------------|-------------:|--------------:|---------:|----------:|--------|")

    ok = mismatch = missing = skipped = 0
    for key, billed in sorted(billing.items()):
        or_id = _billing_key_to_or(key)
        if not or_id:
            lines.append(
                f"| `{key}` | {billed['input']:.3f} | {billed['output']:.3f} "
                f"| — | — | ⏭ no OR mapping |"
            )
            skipped += 1
            continue
        live = or_prices.get(or_id)
        if not live:
            lines.append(
                f"| `{key}` | {billed['input']:.3f} | {billed['output']:.3f} "
                f"| — | — | ❓ not on OpenRouter |"
            )
            missing += 1
            continue
        in_match  = abs(billed["input"]  - live["input"])  < 0.001
        out_match = abs(billed["output"] - live["output"]) < 0.001
        if in_match and out_match:
            status = "✅ match"
            ok += 1
        else:
            status = "⚠️ MISMATCH"
            mismatch += 1
        lines.append(
            f"| `{key}` | {billed['input']:.3f} | {billed['output']:.3f} "
            f"| {live['input']:.3f} | {live['output']:.3f} | {status} |"
        )

    lines.append(f"\n**Summary:** {ok} match · {mismatch} mismatch · {missing} not found · {skipped} no OR mapping\n")

    # -------------------------------------------------------------------
    # Section 5: cache multiplier audit
    # -------------------------------------------------------------------
    lines.append("\n## billing.py cache audit\n")
    lines.append("Validates `cache_read` / `cache_write` multipliers against expected values per provider.\n")
    lines.append("| billing.py key | cache_read | expected | cache_write | expected | Status |")
    lines.append("|----------------|----------:|----------:|------------:|----------:|--------|")

    c_ok = c_mis = c_skip = 0
    for key, billed in sorted(billing.items()):
        expected = _get_expected_cache(key)
        if expected is None:
            lines.append(
                f"| `{key}` | — | — | — | — | ⏭ no cache expected |"
            )
            c_skip += 1
            continue
        cr_billed = billed.get("cache_read", 0)
        cw_billed = billed.get("cache_write", 0)
        cr_exp = expected.get("cache_read", 0)
        cw_exp = expected.get("cache_write", 0)
        cr_match = abs(cr_billed - cr_exp) < 0.001
        cw_match = abs(cw_billed - cw_exp) < 0.001
        if cr_match and cw_match:
            status = "✅ match"
            c_ok += 1
        else:
            status = "⚠️ MISMATCH"
            c_mis += 1
        cr_exp_s = f"{cr_exp:.2f}" if cr_exp else "—"
        cw_exp_s = f"{cw_exp:.2f}" if cw_exp else "—"
        cr_b_s = f"{cr_billed:.2f}" if cr_billed else "—"
        cw_b_s = f"{cw_billed:.2f}" if cw_billed else "—"
        lines.append(
            f"| `{key}` | {cr_b_s} | {cr_exp_s} | {cw_b_s} | {cw_exp_s} | {status} |"
        )

    lines.append(f"\n**Cache summary:** {c_ok} match · {c_mis} mismatch · {c_skip} no cache\n")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(out_path: str) -> None:
    print("Fetching OpenRouter prices...", flush=True)
    or_prices = await _fetch_openrouter_prices()
    print(f"  {len(or_prices)} models fetched")

    print("Fetching OpenAI model list...", flush=True)
    openai_ids = await _fetch_openai_model_ids()
    print(f"  {len(openai_ids)} models")

    print("Fetching Gemini model list...", flush=True)
    gemini_ids = _fetch_gemini_model_ids()
    print(f"  {len(gemini_ids)} models")

    print("Loading billing.py...", flush=True)
    billing = _load_billing_entries()
    print(f"  {len(billing)} entries")

    gemini_aliases = {}
    if gemini_ids:
        aliases_to_resolve = [k for k in billing if k.startswith("gemini-") and k.endswith("-latest")]
        if aliases_to_resolve:
            print(f"Resolving {len(aliases_to_resolve)} Gemini aliases...", flush=True)
            gemini_aliases = _resolve_gemini_aliases(
                [f"models/{a}" for a in aliases_to_resolve]
            )
            for alias, resolved in gemini_aliases.items():
                print(f"  {alias} → {resolved}")

    openai_aliases = {}
    if openai_ids:
        aliases_to_resolve = [k for k in billing if k.startswith("gpt-5.") and not re.search(r"-\d{4}-\d{2}-\d{2}$", k)]
        if aliases_to_resolve:
            print(f"Resolving {len(aliases_to_resolve)} OpenAI aliases...", flush=True)
            openai_aliases = await _resolve_openai_aliases(aliases_to_resolve)
            for alias, resolved in openai_aliases.items():
                print(f"  {alias} → {resolved}")

    report = _build_report(or_prices, openai_ids, gemini_ids, billing, gemini_aliases, openai_aliases)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nReport written to: {out_path}")

    # Print audit summaries to stdout for Makefile feedback
    for line in report.splitlines():
        if line.startswith("**Summary:**") or line.startswith("**Cache summary:**"):
            print(line)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(__file__), "..", "memory", "pricing_report.md"),
        help="Output file path",
    )
    args = parser.parse_args()
    asyncio.run(main(os.path.abspath(args.out)))
