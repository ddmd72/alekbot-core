#!/usr/bin/env python3
"""
Consolidation A/B — Claude Sonnet 5  vs  OpenAI GPT-5.6-terra (cross-provider)
=============================================================================
Runs the REAL ConsolidationAgent (full tool loop, real fact reads, intercepted
writes — nothing is written) twice over the SAME input, once pinned to each
provider at PERFORMANCE tier, and prints the two runs side by side.

Provider is switched faithfully: after the agent is built we re-resolve its
execution context via container.context_builder.resolve_for_task(..., provider_override)
and swap self._llm / self.model_name / self._agent_execution_context. This is the
same production resolution path (caching + alerting proxies applied), not a raw call.

Two scenarios (choose with --mode):
  • dedup  — top-N longest facts from live Firestore → cluster quality audit
             (duplications / inconsistencies / atomicity). Reuses the proven
             test_cluster_audit_dryrun harness. Default N=10.
  • chat   — the REAL raw conversation window that drove the last Stage-1
             consolidation (pulled from BigQuery, saved to
             scripts/memory/last_chat_messages.json) → fact extraction.
  • both   — run dedup then chat (default).

Models: PERFORMANCE tier → claude-sonnet-5 / gpt-5.6-terra. CLAUDE_PERFORMANCE_MODEL
is pinned to claude-sonnet-5 so the Claude leg is deterministic regardless of env.

Pre-conditions:
    .env with DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE, ANTHROPIC + OPENAI keys.
    For --mode chat/both: scripts/memory/last_chat_messages.json must exist.

NOTE: real LLM calls on BOTH models on every run — spends API budget. Run deliberately.

Usage:
    python scripts/consolidation/ab_cross_provider.py                 # both scenarios
    python scripts/consolidation/ab_cross_provider.py --mode dedup --limit 10
    python scripts/consolidation/ab_cross_provider.py --mode chat
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Pin the Claude PERFORMANCE model BEFORE any adapter is constructed.
os.environ.setdefault("CLAUDE_PERFORMANCE_MODEL", "claude-sonnet-5")

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.composition.service_container import ServiceContainer
from src.composition.user_agent_factory import UserAgentFactory
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.complexity_settings import ComplexitySettings
from src.domain.request_context import RequestContext
from src.domain.user import PerformanceTier
from src.infrastructure.agent_coordinator import AgentCoordinator

# Reuse the proven cluster-audit harness helpers verbatim.
from scripts.consolidation.test_cluster_audit_dryrun import (
    DryRunFactManagementAdapter,
    extract_operations_slim,
    extract_reasoning_log,
    fetch_long_facts,
    parse_agent_report,
    run_batch,
)

_MESSAGES_FILE = Path("scripts/memory/last_chat_messages.json")
_OUT_DIR = Path("scripts/memory/consolidation")

# (provider_name for the strategy override, PerformanceTier, human label)
_OPENAI_TIER = {
    "terra": (PerformanceTier.PERFORMANCE, "gpt-5.6-terra"),
    "sol": (PerformanceTier.ULTRA, "gpt-5.6-sol"),
}


_CLAUDE_MODEL = {
    "sonnet5": (PerformanceTier.PERFORMANCE, "claude-sonnet-5", None),
    "opus": (PerformanceTier.ULTRA, "claude-opus-4-8", None),
    "fable": (PerformanceTier.PERFORMANCE, "claude-fable-5", "claude-fable-5"),
}


def build_providers(openai_model: str, claude_model: str):
    o_tier, o_label = _OPENAI_TIER[openai_model]
    c_tier, c_label, c_override = _CLAUDE_MODEL[claude_model]
    return (
        ("claude", c_tier, c_label, c_override),
        ("openai", o_tier, o_label, None),
        ("gemini", PerformanceTier.PERFORMANCE, "gemini-pro-latest (→3.1-pro)", None),
    )


def pin_provider(agent, container, config, provider_name: str,
                 tier: PerformanceTier = PerformanceTier.PERFORMANCE,
                 model_override: str = None) -> str:
    """Faithfully re-point the built agent at `provider_name` @ `tier` (optional model override)."""
    ctx = container.context_builder.resolve_for_task(
        "consolidation",
        config,
        ComplexitySettings(tier=tier, provider_override=provider_name),
    )
    if model_override:
        ctx.model_name = model_override
    # Disable cross-provider fallback: a transient error must fail loudly as THIS
    # provider, not silently switch to the strategy fallback (gemini) and corrupt
    # the transcript mid tool-loop (mixed raw_content → call_id resolution breaks).
    ctx.fallback_provider = None
    ctx.fallback_provider_name = None
    agent._llm = ctx.provider
    agent.model_name = ctx.model_name
    agent._agent_execution_context = ctx
    return ctx.model_name


def _silence_side_effects(agent) -> None:
    """Suppress cache refresh / invalidation writes (dry-run purity)."""
    async def _noop_async(*a, **k):
        pass

    def _noop(*a, **k):
        pass

    try:
        agent._repo.refresh_biographical_context_cache = _noop_async
    except Exception:
        pass
    if getattr(agent, "prompt_builder", None):
        try:
            agent.prompt_builder.invalidate_biographical_cache = _noop
        except Exception:
            pass


async def run_chat(
    agent, messages: List[Dict], bio_facts: List[Dict], user_id: str, account_id: str
) -> Tuple[List[Dict], List[Dict], List[Dict], float]:
    """Stage-1 fact extraction over the real conversation window. Writes intercepted."""
    real_fm = agent._fact_management
    if hasattr(real_fm, "_real"):
        real_fm = real_fm._real
    dry_run = DryRunFactManagementAdapter(real_fm)
    agent._fact_management = dry_run

    llm_turns: List[Dict[str, Any]] = []
    usage = {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
    _orig = agent._call_llm

    async def _capturing(request, turn=None):
        resp = await _orig(request, turn=turn)
        u = getattr(resp, "usage_metadata", None)
        if u:
            usage["prompt"] += u.prompt_tokens or 0
            usage["completion"] += u.completion_tokens or 0
            usage["cache_read"] += u.cache_read_tokens or 0
            usage["cache_write"] += u.cache_creation_tokens or 0
            usage["calls"] += 1
        llm_turns.append({
            "turn": turn,
            "text": resp.text or "",
            "tool_calls": [{"name": tc.name, "args": tc.args} for tc in (resp.tool_calls or [])],
        })
        return resp

    agent._call_llm = _capturing

    message = AgentMessage.create(
        sender="ab_chat_script",
        recipient=f"consolidation_agent_{user_id}",
        intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": messages, "biographical_context": bio_facts},
        context={"user_id": user_id, "account_id": account_id, "session_id": "ab_chat"},
    )

    t0 = time.time()
    async with RequestContext(user_id=user_id, account_id=account_id):
        await agent.execute(message)
    elapsed = time.time() - t0

    agent._call_llm = _orig

    # Per-1M-token pricing (USD, ABSOLUTE $/M): input, output, cache_read, cache_write.
    # Claude cache is 0.1x input (read) / 1.25x input (5-min write) — folded in here.
    _PRICE = {
        "claude-sonnet-5": (3.00, 15.00, 0.30, 3.75),
        "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
        "claude-fable-5": (10.00, 50.00, 1.00, 12.50),
        "gpt-5.6-terra": (1.25, 10.00, 0.125, 0.0),
        "gpt-5.6-sol": (2.50, 20.00, 0.25, 0.0),
        "gemini-pro-latest": (2.00, 12.00, 0.50, 0.0),
    }
    pin, pout, prd, pwr = _PRICE.get(agent.model_name, (0, 0, 0, 0))
    cost = (usage["prompt"] * pin + usage["completion"] * pout
            + usage["cache_read"] * prd + usage["cache_write"] * pwr) / 1_000_000
    print(f"    💰 [{agent.model_name}] calls={usage['calls']}  in={usage['prompt']}  "
          f"cache_read={usage['cache_read']}  cache_write={usage['cache_write']}  "
          f"out={usage['completion']}  →  ${cost:.4f}")

    # Dump the raw per-turn transcript (text + tool_calls) for independent verification
    # of what the model actually emitted each turn — before any parsing.
    import re as _re
    tag = _re.sub(r"[^a-z0-9]+", "_", agent.model_name.lower())
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    (_OUT_DIR / f"turns_chat_{tag}.json").write_text(
        json.dumps(llm_turns, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = parse_agent_report(llm_turns)
    return extract_reasoning_log(report), extract_operations_slim(dry_run.operations), dry_run.operations, elapsed


def _print_run(label: str, model: str, reasoning: List[Dict], ops: List[Dict], elapsed: float) -> None:
    counts = Counter(o["action"] for o in ops)
    print(f"\n----- {label} ({model}) — {elapsed:.1f}s -----")
    print("  " + "  ".join(f"{a}:{counts.get(a, 0)}" for a in ["CREATE", "UPDATE", "MERGE", "DISCARD"]))
    for r in reasoning:
        print(f"    [{r['action']}] {r['reason'][:150]}")
    for o in ops:
        act = o.get("action")
        if act == "CREATE":
            print(f"    + CREATE: {o.get('content', '')[:120]}")
        elif act == "MERGE":
            print(f"    ~ MERGE {o.get('old_ids', [])}: {o.get('content', '')[:100]}")
        elif act == "UPDATE":
            print(f"    * UPDATE {o.get('fact_id')}: {str(o.get('content', ''))[:100]}")
        elif act == "DISCARD":
            print(f"    - DISCARD: {o.get('reason', '')[:100]}")


async def main(mode: str, limit: int, user_id: str, account_id: str, openai_model: str, only: str, claude_model: str) -> None:
    providers = build_providers(openai_model, claude_model)
    if only == "both":
        providers = tuple(p for p in providers if p[0] != "gemini")  # both = claude+openai
    else:
        providers = tuple(p for p in providers if p[0] == only)
    database_id = os.getenv("FIRESTORE_DATABASE", "us-production")
    db = firestore.AsyncClient(database=database_id)
    config_settings = load_settings()
    env_config = config_settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    coordinator = AgentCoordinator()
    container = ServiceContainer(config=config_settings, db_client=db, env_config=env_config, account_repo=account_repo)

    profile = await user_repo.get_user(user_id)
    if not profile:
        print(f"ERROR: user {user_id} not found.")
        return
    user_config = profile.config

    factory = UserAgentFactory(
        config=config_settings, env_config=env_config, coordinator=coordinator,
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    print("Creating agents...")
    agents = await factory.ensure_agents_for_user(user_id)
    agent = agents.get("consolidation_agent")
    if not agent:
        print(f"consolidation_agent not found. Keys: {list(agents.keys())}")
        return
    _silence_side_effects(agent)

    bio_facts: List[Dict] = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(account_id, limit=100)
        print(f"Loaded {len(bio_facts)} biographical facts for context.")
    except Exception as e:
        print(f"⚠️  bio load failed: {e}")

    session_id = "ab_cross"
    try:
        session_id = await container.session_store.get_latest_session_id(user_id) or session_id
    except Exception:
        pass

    results: Dict[str, Any] = {"generated_at": datetime.now().isoformat(), "runs": {}}

    # ── Scenario: dedup ──────────────────────────────────────────────
    if mode in ("dedup", "both"):
        facts = await fetch_long_facts(container.repository, account_id, min_words=1, limit=limit)
        print(f"\n{'='*72}\nSCENARIO A — DEDUP (top {len(facts)} longest facts)\n{'='*72}")
        for provider_name, tier, model_label, model_override in providers:
            model = pin_provider(agent, container, user_config, provider_name, tier, model_override)
            print(f"\n### {provider_name} → {model}  [start {datetime.now().strftime('%H:%M:%S')}]")
            reasoning, ops, raw, elapsed = await run_batch(
                agent, facts, 0, bio_facts, user_id, account_id, session_id
            )
            _print_run(f"DEDUP/{provider_name}", model, reasoning, ops, elapsed)
            results["runs"].setdefault("dedup", {})[provider_name] = {
                "model": model, "elapsed_s": round(elapsed, 1),
                "reasoning": reasoning, "operations": ops,
            }

    # ── Scenario: chat ───────────────────────────────────────────────
    if mode in ("chat", "both"):
        if not _MESSAGES_FILE.exists():
            print(f"\n⚠️  {_MESSAGES_FILE} missing — skipping chat scenario.")
        else:
            messages = json.loads(_MESSAGES_FILE.read_text())
            print(f"\n{'='*72}\nSCENARIO B — CHAT EXTRACTION ({len(messages)} real turns)\n{'='*72}")
            for provider_name, tier, model_label, model_override in providers:
                model = pin_provider(agent, container, user_config, provider_name, tier, model_override)
                print(f"\n### {provider_name} → {model}  [start {datetime.now().strftime('%H:%M:%S')}]")
                reasoning, ops, raw, elapsed = await run_chat(agent, messages, bio_facts, user_id, account_id)
                _print_run(f"CHAT/{provider_name}", model, reasoning, ops, elapsed)
                results["runs"].setdefault("chat", {})[provider_name] = {
                    "model": model, "elapsed_s": round(elapsed, 1),
                    "reasoning": reasoning, "operations": ops,
                }

    # ── Persist ──────────────────────────────────────────────────────
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = _OUT_DIR / f"ab_cross_provider_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{'='*72}\nFull results → {out}\n{'='*72}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Consolidation cross-provider A/B (Sonnet 5 vs GPT-5.6-terra)")
    ap.add_argument("--mode", choices=["dedup", "chat", "both"], default="both")
    ap.add_argument("--limit", type=int, default=10, help="dedup: number of longest facts (default 10)")
    ap.add_argument("--openai-model", choices=["terra", "sol"], default="terra",
                    help="OpenAI leg: terra=PERFORMANCE (gpt-5.6-terra), sol=ULTRA (gpt-5.6-sol)")
    ap.add_argument("--only", choices=["both", "claude", "openai", "gemini"], default="both",
                    help="Run only one provider leg (default both = claude+openai)")
    ap.add_argument("--claude-model", choices=["sonnet5", "opus", "fable"], default="sonnet5",
                    help="Claude leg: sonnet5 (PERFORMANCE), opus (ULTRA→opus-4-8), fable (claude-fable-5)")
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    a = ap.parse_args()
    if not a.user_id or not a.account_id:
        print("ERROR: set DEV_USER_ID / DEV_ACCOUNT_ID in .env or pass --user-id/--account-id")
        sys.exit(1)
    asyncio.run(main(a.mode, a.limit, a.user_id, a.account_id, a.openai_model, a.only, a.claude_model))
