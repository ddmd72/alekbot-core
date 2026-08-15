#!/usr/bin/env python3
"""
HtmlPageGenerator on Grok — cost and output measurement
=======================================================
Runs the REAL HtmlPageGeneratorAgent, pinned to grok @ PERFORMANCE (grok-4.6),
over the EXACT input Gemini already handled in production, and reports what it
cost and what it produced.

Fixed input: the `create_html_page` delegation Smart issued during the briefing
run of 2026-08-15 09:45 UTC — 24,546 chars of assembled content, the Ukrainian
"Ранковий Вісник" edition. Saved to scripts/memory/html_page_input_2026-08-15.json
alongside the Gemini baseline it is compared against, so the comparison is against
a real production run rather than a re-run.

Nothing is published: the agent returns the HTML inside a DeliveryItem (the GCS
upload happens downstream in the delivery funnel, not here), so this script just
decodes it and writes to scripts/memory/ — gitignored, per the repo's PII rule.

Provider is switched faithfully via the production resolution path
(container.context_builder.resolve_for_task with provider_override), the same
mechanism scripts/consolidation/ab_cross_provider.py uses. Note that `html_page`
does NOT list grok in AgentProviderStrategy.allowed_providers — that list is only
consulted by resolve_provider_name and resolve_next_provider, never by _build, so
pinning works without touching production config. Decide on allowed_providers
AFTER seeing these numbers.

Cross-provider fallback is disabled on the pinned context: `html_page` falls back
to claude, and a transient grok error would otherwise silently produce a Claude
page reported as grok.

NOTE: one real LLM call on grok-4.6 with a 24.5k-char prompt and up to 64k output
tokens. Spends real budget. Run deliberately.

Usage:
    python scripts/html_page/ab_grok.py
    python scripts/html_page/ab_grok.py --provider gemini   # re-measure the baseline
    python scripts/html_page/ab_grok.py --tier balanced     # grok-4.3 instead
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.composition.service_container import ServiceContainer
from src.composition.user_agent_factory import UserAgentFactory
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.billing import calculate_cost
from src.domain.complexity_settings import ComplexitySettings
from src.domain.user import PerformanceTier
from src.infrastructure.agent_coordinator import AgentCoordinator

INPUT_FILE = Path(__file__).parent.parent / "memory" / "html_page_input_2026-08-15.json"
OUT_DIR = Path(__file__).parent.parent / "memory"

_TIERS = {
    "eco": PerformanceTier.ECO,
    "balanced": PerformanceTier.BALANCED,
    "performance": PerformanceTier.PERFORMANCE,
    "ultra": PerformanceTier.ULTRA,
}


def pin_provider(agent, container, config, provider_name: str, tier: PerformanceTier) -> str:
    """Re-point the built agent at `provider_name` @ `tier` via the production path."""
    ctx = container.context_builder.resolve_for_task(
        "html_page",
        config,
        ComplexitySettings(tier=tier, provider_override=provider_name),
    )
    # A transient error must fail loudly as THIS provider, not silently become the
    # strategy fallback (claude) and be reported as grok.
    ctx.fallback_provider = None
    ctx.fallback_provider_name = None
    agent._llm = ctx.provider
    agent.model_name = ctx.model_name
    agent._agent_execution_context = ctx
    return ctx.model_name


def capture_usage(agent) -> dict:
    """Wrap _call_llm to record what the provider actually reported.

    Wrapping here (not _call_llm_recitation_aware) also counts a recitation retry,
    which would otherwise silently double the real cost.
    """
    usage = {"calls": 0, "prompt": 0, "completion": 0, "cached": 0, "models": []}
    original = agent._call_llm

    async def _capturing(request, *a, **kw):
        response = await original(request, *a, **kw)
        usage["calls"] += 1
        usage["models"].append(request.model_name)
        um = response.usage_metadata
        if um:
            usage["prompt"] += um.prompt_tokens or 0
            usage["completion"] += um.completion_tokens or 0
            usage["cached"] += getattr(um, "cache_read_tokens", 0) or 0
        return response

    agent._call_llm = _capturing
    return usage


async def main(provider: str, tier_name: str, user_id: str, account_id: str) -> None:
    if not INPUT_FILE.exists():
        print(f"ERROR: fixed input missing: {INPUT_FILE}")
        return
    payload = json.loads(INPUT_FILE.read_text())
    query = payload["query"]
    baseline = payload.get("gemini_baseline", {})
    tier = _TIERS[tier_name]

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    config_settings = load_settings()
    env_config = config_settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(
        db_client=db, collection_name=env_config.account_collection_name
    )
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    coordinator = AgentCoordinator()
    container = ServiceContainer(
        config=config_settings, db_client=db, env_config=env_config, account_repo=account_repo
    )

    profile = await user_repo.get_user(user_id)
    if not profile:
        print(f"ERROR: user {user_id} not found.")
        return

    factory = UserAgentFactory(
        config=config_settings, env_config=env_config, coordinator=coordinator,
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    print("Creating agents...")
    await factory.ensure_agents_for_user(user_id)
    # html_page is lazy (eager=False) — created on first delegation in production.
    await factory.create_agent_on_demand("html_page", user_id)
    agent = coordinator.get_agent(f"html_page_generator_agent_{user_id}")
    if agent is None:
        print("ERROR: html_page agent not created.")
        return

    model = pin_provider(agent, container, profile.config, provider, tier)
    usage = capture_usage(agent)

    print(f"\n{'='*70}")
    print(f"  provider   : {provider} @ {tier_name}  →  {model}")
    print(f"  input      : {len(query)} chars (production briefing 2026-08-15)")
    print(f"  params     : temperature={agent.TEMPERATURE} max_tokens={agent.MAX_TOKENS} "
          f"thinking={agent.THINKING_EFFORT}")
    print(f"{'='*70}\n  running...", flush=True)

    message = AgentMessage.create(
        sender="ab_bench",
        recipient=agent.agent_id,
        intent=AgentIntent.QUERY,
        payload={"query": query},
        context={"account_id": account_id, "user_id": user_id},
    )

    started = time.perf_counter()
    try:
        response = await agent.execute(message)
    except Exception as exc:
        print(f"\n  ❌ FAILED after {time.perf_counter()-started:.1f}s: {type(exc).__name__}: {exc}")
        return
    elapsed = time.perf_counter() - started

    if not response.delivery_items:
        print(f"\n  ❌ no delivery items — status={response.status} error={response.error}")
        return

    data = response.delivery_items[0].data
    html = base64.b64decode(data["content_b64"]).decode("utf-8")

    ran = set(usage["models"])
    contaminated = ran - {model}

    cost = calculate_cost(
        model=model,
        prompt_tokens=usage["prompt"],
        completion_tokens=usage["completion"],
        cache_read_tokens=usage["cached"],
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "_", model.lower())
    out = OUT_DIR / f"html_page_{slug}_{stamp}.html"
    out.write_text(html, encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"  RESULT — {model}")
    print(f"{'='*70}")
    print(f"  wall time        : {elapsed:.1f}s")
    print(f"  llm calls        : {usage['calls']}"
          + ("  ⚠️ >1 means a recitation retry fired" if usage["calls"] > 1 else ""))
    print(f"  prompt tokens    : {usage['prompt']:,} (uncached)")
    print(f"  cached tokens    : {usage['cached']:,}")
    print(f"  completion tokens: {usage['completion']:,}")
    print(f"  COST             : ${cost:.4f}")
    print(f"  html size        : {len(html):,} chars")
    print(f"  filename chosen  : {data.get('filename')}")
    print(f"  saved to         : {out}")
    if contaminated:
        print(f"  ⚠️ CONTAMINATED  : calls also ran on {contaminated} — result is not pure {model}")

    if baseline and provider != "gemini":
        b_cost = baseline.get("cost_usd", 0)
        print(f"\n  vs {baseline.get('model')} baseline (same input, production run):")
        print(f"    cost       ${b_cost:.4f}  →  ${cost:.4f}"
              + (f"   ({cost/b_cost:.2f}x)" if b_cost else ""))
        print(f"    prompt tok {baseline.get('prompt_tokens'):,}  →  {usage['prompt']:,}")
        print(f"    output tok {baseline.get('completion_tokens'):,}  →  {usage['completion']:,}")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="grok")
    ap.add_argument("--tier", default="performance", choices=list(_TIERS))
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    args = ap.parse_args()
    if not args.user_id or not args.account_id:
        print("ERROR: DEV_USER_ID / DEV_ACCOUNT_ID missing (.env or --user-id/--account-id)")
        sys.exit(1)
    asyncio.run(main(args.provider, args.tier, args.user_id, args.account_id))
