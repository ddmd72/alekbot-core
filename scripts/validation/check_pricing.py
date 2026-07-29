"""
Model pricing audit: two-source consensus against billing.py.

VOTING sources — both track provider LIST prices, i.e. what we are actually billed:
  - LiteLLM     model_prices_and_context_window.json (BerriAI/litellm, raw GitHub)
  - models.dev  GET https://models.dev/api.json

NON-VOTING, reference only:
  - OpenRouter  GET https://openrouter.ai/api/v1/models

  OpenRouter is a RESELLER quoting its own rates. It used to be the sole reference, which
  on 2026-07-29 produced 6 wrong verdicts out of 8 — e.g. it called `gpt-5.6-luna`
  ($1.00/$6.00, confirmed by both catalogs and OpenAI's own docs) a mismatch because it
  resells at $0.50/$3.00. A materially lower OR price is an arbitrage lead, never a
  correction. It is kept in the table for exactly that.

Supporting lookups:
  - OpenAI      GET /v1/models + a minimal completion per alias → resolved model id
  - Gemini      client.models.list() + a minimal generate per alias → model_version

  Alias resolution is LOAD-BEARING: the catalogs key on concrete models, and the previous
  audit resolved aliases live but then compared prices through a stale hardcoded map,
  yielding false ✅ verdicts for every Gemini `*-latest` entry.

The verdict rule and the dated PRICE_SCHEDULE live in `price_consensus.py` (pure, unit
tested in tests/unit/validation/test_price_consensus.py). Read that module before changing
any price.

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
from datetime import datetime, timezone

import truststore
from dotenv import load_dotenv

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import price_consensus  # noqa: E402  (pure logic, no side effects)
from price_consensus import Price  # noqa: E402

truststore.inject_into_ssl()  # trust the OS keychain (e.g. Charles CA) before any TLS client is built
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


# models.dev sits behind a CDN that 403s the default Python-urllib User-Agent.
_UA = {"User-Agent": "alekbot-pricing-audit/1.0 (+https://github.com/ddmd72/alekbot-core)"}


def _get_json(url: str, timeout: int = 60):
    """GET + parse JSON with a real User-Agent."""
    import json
    import urllib.request
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _fetch_litellm_prices() -> dict[str, Price]:
    """LiteLLM's price catalog → {model_id: (input, output)} per million tokens.

    A voting source: it tracks provider LIST prices, which is what we are billed.
    Keys are sometimes bare (`gpt-5.6-luna`) and sometimes provider-scoped
    (`vertex_ai/gemini-...`); both forms are indexed under the bare id.
    """
    url = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
           "model_prices_and_context_window.json")
    try:
        data = _get_json(url)
    except Exception as e:
        # A source outage must degrade to "single source → review", never kill the audit.
        print(f"  [warn] LiteLLM catalog unavailable: {e}", file=sys.stderr)
        return {}

    out: dict[str, Price] = {}
    for key, v in data.items():
        if not isinstance(v, dict) or v.get("input_cost_per_token") is None:
            continue
        bare = key.split("/")[-1]
        price = (round(float(v["input_cost_per_token"]) * 1e6, 4),
                 round(float(v.get("output_cost_per_token") or 0) * 1e6, 4))
        out.setdefault(bare, price)
    return out


def _fetch_modelsdev_prices() -> dict[str, Price]:
    """models.dev catalog → {model_id: (input, output)} per million tokens.

    The second voting source. Shape: {provider: {models: {model_id: {cost: {...}}}}}.
    """
    try:
        data = _get_json("https://models.dev/api.json")
    except Exception as e:
        print(f"  [warn] models.dev catalog unavailable: {e}", file=sys.stderr)
        return {}

    out: dict[str, Price] = {}
    for provider in data.values():
        for mid, m in (provider.get("models") or {}).items():
            cost = m.get("cost") or {}
            if cost.get("input") is None:
                continue
            out.setdefault(mid, (round(float(cost["input"]), 4),
                                 round(float(cost.get("output") or 0), 4)))
    return out


def _lookup_keys(billing_key: str, resolved: dict[str, str]) -> list[str]:
    """Model ids to price this billing.py entry against, best first.

    Alias resolution is LOAD-BEARING, not informational: catalogs key on concrete models,
    so a `*-latest` alias either misses entirely or gets priced as whichever generation
    each catalog happened to resolve it to. That is what produced three bogus
    SOURCES_DISAGREE verdicts before this existed.

    The bare key is added as a FALLBACK only when the resolved id is a dated snapshot of
    the same model (`gpt-5.4` → `gpt-5.4-2026-03-05`) — catalogs often carry only the
    undated id, and both name the same thing. It is deliberately NOT added for a moving
    alias (`gemini-flash-lite-latest` → `gemini-3.5-flash-lite`), where the alias and the
    target are different models and falling back would resurrect the original bug.
    """
    target = resolved.get(billing_key, billing_key)
    if target.startswith("error:"):          # live resolution failed — fall back
        target = billing_key
    if target.startswith("models/"):         # Gemini ids come back prefixed
        target = target[len("models/"):]
    if target == billing_key:
        return [target]
    if target.startswith(billing_key):       # dated snapshot of the same model
        return [target, billing_key]
    return [target]


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
    # GPT-5.6 bills cache WRITES at 1.25x uncached input (the 5.4/5.5 families do not).
    # OpenAI: "Cache writes cost 1.25x the uncached input rate, with a 30-minute minimum
    # cache life." Longest prefix wins, so this beats the generic "gpt-" entry below.
    "gpt-5.6-":        {"cache_read": 0.10, "cache_write": 1.25},
    "gpt-":            {"cache_read": 0.10},
    "o3-":             {"cache_read": 0.10},
    "o4-":             {"cache_read": 0.10},
}


def _get_expected_cache(key: str) -> dict[str, float] | None:
    """Return expected cache multipliers for a billing key, or None if no cache expected.

    LONGEST matching prefix wins — "gpt-5.6-" must beat "gpt-", otherwise dict order
    decides and the 5.6 family gets judged by the generic no-cache-write expectation.
    """
    matches = [(len(p), e) for p, e in _EXPECTED_CACHE.items() if key.startswith(p)]
    return max(matches)[1] if matches else None


_BILLING_TO_OR: dict[str, str] = {
    # Gemini aliases → resolved via generate call (model_version field)
    "gemini-flash-lite-latest":          "google/gemini-2.5-flash-lite",
    "gemini-flash-latest":               "google/gemini-3.5-flash",  # live alias → gemini-3.5-flash
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


def _first_quote(catalog: dict[str, Price], candidates: list[str]) -> Price | None:
    """First candidate id this catalog knows about."""
    for c in candidates:
        if c in catalog:
            return catalog[c]
    return None


def _p(price: "Price | None") -> str:
    """Table cell for an optional price pair."""
    return f"{price[0]:g}/{price[1]:g}" if price else "—"


def _build_report(
    or_prices: dict[str, dict],
    openai_ids: list[str],
    gemini_ids: list[str],
    billing: dict[str, dict],
    litellm_prices: dict[str, Price],
    modelsdev_prices: dict[str, Price],
    gemini_aliases: dict[str, str] | None = None,
    openai_aliases: dict[str, str] | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    lines.append(f"# Model Pricing Report — {now}\n")
    lines.append("Generated by `make check-pricing`.\n")
    lines.append("> Prices per million tokens (USD). Voting sources: LiteLLM + models.dev. OpenRouter is reference-only.\n")

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
        lines.append("| Model ID | Input $/M | Output $/M |")
        lines.append("|----------|----------:|----------:|")
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
    lines.append("## billing.py audit — two-source consensus\n")
    lines.append(
        "Voting sources: **LiteLLM** and **models.dev**, both tracking provider LIST prices "
        "(what we are billed). Agreement confirms; disagreement, single coverage and no "
        "coverage all go to review. A dated `PRICE_SCHEDULE` entry overrides consensus — the "
        "catalogs publish today's number with no expiry.\n"
    )
    lines.append(
        "> OpenRouter is shown for reference only and does **not** vote: it is a reseller "
        "quoting its own rates. Treating it as truth caused 6 wrong verdicts on 2026-07-29. "
        "A materially lower OR price is an arbitrage lead, not a correction.\n"
    )
    lines.append("| billing.py key | priced as | ours | LiteLLM | models.dev | OR (fyi) | Verdict |")
    lines.append("|----------------|-----------|-----:|--------:|-----------:|---------:|---------|")

    _ICON = {
        price_consensus.CONFIRMED: "✅",
        price_consensus.CONSENSUS_DIFFERS: "⚠️",
        price_consensus.SOURCES_DISAGREE: "🔍",
        price_consensus.SINGLE_SOURCE: "🔍",
        price_consensus.UNCOVERED: "❓",
        price_consensus.SCHEDULE_DRIFT: "⏰",
        price_consensus.SCHEDULE_STALE: "⚠️",
    }
    # Gemini aliases are resolved via "models/<alias>", so strip the prefix from the KEYS
    # too — _lookup_key is called with the bare billing.py key.
    resolved_map = {
        (k[len("models/"):] if k.startswith("models/") else k): v
        for k, v in {**(gemini_aliases or {}), **(openai_aliases or {})}.items()
    }
    today = datetime.now(timezone.utc).date()

    confirmed = 0
    review: list[str] = []
    upcoming: list[str] = []

    for key, billed in sorted(billing.items()):
        candidates = _lookup_keys(key, resolved_map)
        target = candidates[0]
        ours = (billed["input"], billed["output"])
        quotes = {
            "LiteLLM": _first_quote(litellm_prices, candidates),
            "models.dev": _first_quote(modelsdev_prices, candidates),
        }
        v = price_consensus.resolve_verdict(target, ours, quotes, today)

        or_p = or_prices.get(_billing_key_to_or(key) or "")
        cells = [
            f"`{key}`",
            f"`{target}`" if target != key else "—",
            _p(ours),
            _p(quotes["LiteLLM"]),
            _p(quotes["models.dev"]),
            f"{or_p['input']:g}/{or_p['output']:g}" if or_p else "—",
            f"{_ICON.get(v.status, '')} {v.status}",
        ]
        lines.append("| " + " | ".join(cells) + " |")

        if v.status == price_consensus.CONFIRMED:
            confirmed += 1
        else:
            review.append(f"- `{key}` — **{v.status}**: {v.detail}")
        if v.upcoming:
            when, price = v.upcoming
            upcoming.append(
                f"- `{key}` → {price_consensus._fmt(price)} effective **{when.isoformat()}** "
                f"(update billing.py on that date; this audit will flag it as `schedule_drift`)"
            )

    lines.append(
        f"\n**Summary:** {confirmed} confirmed · {len(review)} need review "
        f"(of {len(billing)} entries)\n"
    )
    if review:
        lines.append("### Needs review\n")
        lines.extend(sorted(set(review)))
        lines.append("")
    if upcoming:
        lines.append("### Scheduled price changes\n")
        lines.extend(sorted(set(upcoming)))
        lines.append("")

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

    print("Fetching LiteLLM price catalog...", flush=True)
    litellm_prices = _fetch_litellm_prices()
    print(f"  {len(litellm_prices)} models")

    print("Fetching models.dev price catalog...", flush=True)
    modelsdev_prices = _fetch_modelsdev_prices()
    print(f"  {len(modelsdev_prices)} models")

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

    report = _build_report(
        or_prices, openai_ids, gemini_ids, billing,
        litellm_prices, modelsdev_prices, gemini_aliases, openai_aliases,
    )

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
