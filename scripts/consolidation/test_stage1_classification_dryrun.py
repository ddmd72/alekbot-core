#!/usr/bin/env python3
"""
Stage 1 Dry-Run — AGENT_DIRECTIVE vs PREFERENCE classification
===============================================================
Runs the REAL Stage-1 consolidation (fact extraction from a real conversation
window) with fact writes intercepted, and prints every created/updated fact with
its DOMAIN — the thing the classification rule decides.

WHY
---
`Directive_Maintenance.classification` in the Firestore consolidation prompt
currently routes by SUBJECT only: "user instructs HOW THE AGENT must behave ->
AGENT_DIRECTIVE, user's own habits -> PREFERENCE". Measured 2026-08-17, that
sends situational agent rules (document formatting, transport preference) into
the always-injected rulebook, where they are binding on every unrelated request.

`--rule new` swaps in a two-axis classification (SUBJECT × SCOPE) plus a clause
telling the consolidator that recurring tasks already carry their own execution
protocol in the reminder system and must not be restated as facts. The rule text
lives HERE, not in Firestore: this decides whether it earns the write.

The swap is a string replacement on the ASSEMBLED prompt, so nothing in
`development_prompt_components` is touched and no deploy is involved.

Usage:
    python scripts/consolidation/test_stage1_classification_dryrun.py --rule baseline
    python scripts/consolidation/test_stage1_classification_dryrun.py --rule new
    python scripts/consolidation/test_stage1_classification_dryrun.py --rule both --messages 50

Pre-conditions:
    .env with DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE + provider keys.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.composition.service_container import ServiceContainer
from src.composition.user_agent_factory import UserAgentFactory
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.request_context import RequestContext
from src.infrastructure.agent_coordinator import AgentCoordinator

from scripts.consolidation.test_cluster_audit_dryrun import DryRunFactManagementAdapter

# ─────────────────────────────────────────────────────────────────────────────
# Candidate classification (the thing under test)
# ─────────────────────────────────────────────────────────────────────────────

# Matches the `classification: "..."` entry of rule Directive_Maintenance in the
# assembled prompt. Non-greedy up to the closing quote of the value.
_CLASSIFICATION_RE = re.compile(
    r'classification:\s*"User instructs or corrects HOW THE AGENT.*?"',
    re.S,
)

_NEW_CLASSIFICATION = '''classification: "Two tests, both must pass for AGENT_DIRECTIVE.
                        (1) SUBJECT — the user instructs or corrects HOW THE AGENT must behave,
                            reason or respond; not the user's own habits, tastes, values or life
                            principles.
                        (2) SCOPE — the rule is in force across most requests, not only when a
                            specific condition holds.
                        Fails (1) -> PREFERENCE.
                        Passes (1) but fails (2) -> PREFERENCE, rewritten to state the condition it
                            applies under. A conditional instruction to the agent is a preference,
                            not a directive: it reaches the agent by relevance on the requests where
                            it applies."

                    reminders: "The orchestrator owns a reminder tool, and recurring tasks reach it
                        through that separate channel carrying their own execution protocol. Never
                        restate a reminder's protocol, steps or schedule as a fact — that is a
                        duplicate. Record only that the routine exists, and only if that is not
                        already known."'''


def patch_prompt(prompt: str) -> Tuple[str, bool]:
    """Swap the classification rule in an already-assembled prompt."""
    patched, n = _CLASSIFICATION_RE.subn(_NEW_CLASSIFICATION, prompt, count=1)
    return patched, bool(n)


# ─────────────────────────────────────────────────────────────────────────────
# Input: a real conversation window
# ─────────────────────────────────────────────────────────────────────────────

def _part_text(part: Any, role: str) -> str:
    """Mirror the production consolidation serializer.

    Stored history holds `parts`, not a flat `text`: a model part carries both a
    short `text` (summary) and a verbose `full_text`, and consolidation reads the
    summary; a user part prefers `consolidation_text` when present. Reading the
    wrong field silently yields an empty window — which is exactly what happened
    on the first run of this bench.
    """
    get = part.get if isinstance(part, dict) else (lambda k, d=None: getattr(part, k, d))
    if role in ("user", "human"):
        return (get("consolidation_text") or get("text") or "").strip()
    return (get("text") or "").strip()


async def load_window(db, user_id: str, limit: int) -> Tuple[List[Dict[str, Any]], str]:
    """Newest `limit` turns of the user's most recently active session, oldest-first.

    Picks by `last_activity` across the user's sessions rather than trusting a
    default: the per-channel sessions (`{user_id}:{channel_id}`) are the live ones,
    while a legacy channel-less document can be months stale and still be returned.
    """
    newest: Tuple[float, str, List[Any]] = (0.0, "", [])
    async for doc in db.collection("development_sessions").stream():
        x = doc.to_dict() or {}
        if str(x.get("owner_id") or x.get("user_id")) != user_id:
            continue
        ts = float(x.get("last_activity") or 0)
        if ts > newest[0]:
            newest = (ts, doc.id, list(x.get("messages") or x.get("history") or []))

    _, session_id, history = newest
    if not history:
        return [], session_id

    out: List[Dict[str, Any]] = []
    for m in history[-limit:] if limit else history:
        role = (m.get("role") if isinstance(m, dict) else getattr(m, "role", "user")) or "user"
        parts = (m.get("parts") if isinstance(m, dict) else getattr(m, "parts", None)) or []
        text = "\n".join(t for t in (_part_text(p, role) for p in parts) if t)
        if text:
            out.append({"role": role, "text": text, "timestamp": time.time()})
    return out, session_id


# ─────────────────────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────────────────────

async def run_stage1(
    agent, messages: List[Dict], bio_facts: List[Dict],
    user_id: str, account_id: str, use_new_rule: bool,
) -> Tuple[List[Dict[str, Any]], float, int, bool]:
    real_fm = agent._fact_management
    if hasattr(real_fm, "_real"):
        real_fm = real_fm._real
    dry_run = DryRunFactManagementAdapter(real_fm)
    agent._fact_management = dry_run

    patched_ok = {"hit": False}
    original_build = agent.prompt_builder.build_for_agent

    async def building(*args, **kwargs):
        prompt = await original_build(*args, **kwargs)
        if not use_new_rule:
            return prompt
        patched, hit = patch_prompt(prompt)
        patched_ok["hit"] = patched_ok["hit"] or hit
        return patched

    agent.prompt_builder.build_for_agent = building

    tokens = 0
    original_call = agent._call_llm

    async def counting(request, turn=None):
        nonlocal tokens
        resp = await original_call(request, turn=turn)
        u = getattr(resp, "usage_metadata", None)
        if u:
            tokens += (u.prompt_tokens or 0) + (u.completion_tokens or 0)
        return resp

    agent._call_llm = counting

    msg = AgentMessage.create(
        sender="stage1_classification_bench",
        recipient=f"consolidation_agent_{user_id}",
        intent=AgentIntent.DELEGATE,
        payload={"task": "consolidate", "messages": messages, "biographical_context": bio_facts},
        context={"user_id": user_id, "account_id": account_id, "session_id": "stage1_bench"},
    )

    t0 = time.time()
    async with RequestContext(user_id=user_id, account_id=account_id):
        await agent.execute(msg)
    elapsed = time.time() - t0

    agent._call_llm = original_call
    agent.prompt_builder.build_for_agent = original_build
    return dry_run.operations, elapsed, tokens, patched_ok["hit"]


def report(label: str, ops: List[Dict], elapsed: float, tokens: int) -> None:
    print(f"\n{'='*98}\n{label} — {elapsed:.1f}s, {tokens} tokens (uncached in + out), {len(ops)} ops\n{'='*98}")
    by_domain = Counter()
    for n, op in enumerate(ops, 1):
        a = op.get("action")
        if a == "CREATE":
            dom = str((op.get("metadata") or {}).get("domain", "?")).lower()
            by_domain[dom] += 1
            print(f"\n{n:2}. CREATE  domain={dom}\n    {op.get('content','')}")
        elif a == "UPDATE":
            upd = op.get("updates", {})
            print(f"\n{n:2}. UPDATE  {str(op.get('fact_id'))[:8]}\n    {json.dumps(upd, ensure_ascii=False)[:300]}")
        elif a == "MERGE":
            print(f"\n{n:2}. MERGE   {[str(i)[:8] for i in op.get('old_ids',[])]}\n    {op.get('content','')[:300]}")
        elif a == "DISCARD":
            print(f"\n{n:2}. DISCARD {op.get('reason','')[:200]}")
    if by_domain:
        print(f"\n  created by domain: {dict(by_domain)}")


async def main(rule: str, n_messages: int, user_id: str, account_id: str) -> None:
    print(f"\n{'='*98}\nSTAGE 1 CLASSIFICATION DRY-RUN (nothing is written)\n{'='*98}")

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    cfg = load_settings()
    env = cfg["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env.account_collection_name)
    user_repo = FirestoreUserRepository(db, env, account_repo)
    container = ServiceContainer(config=cfg, db_client=db, env_config=env, account_repo=account_repo)
    factory = UserAgentFactory(
        config=cfg, env_config=env, coordinator=AgentCoordinator(),
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    agents = await factory.ensure_agents_for_user(user_id)
    agent = agents.get("consolidation_agent")
    if agent is None or agent._fact_management is None:
        print("  ERROR: consolidation_agent unavailable.")
        return

    async def _noop_async(*a, **k):
        pass

    def _noop(*a, **k):
        pass

    agent._repo.refresh_biographical_context_cache = _noop_async
    if agent.prompt_builder:
        agent.prompt_builder.invalidate_biographical_cache = _noop

    print(f"  model: {agent.model_name}")

    messages, session_id = await load_window(db, user_id, n_messages)
    if not messages:
        print(f"  ERROR: no usable conversation window (session={session_id or 'none'}).")
        return
    print(f"  window: {len(messages)} turns from session {session_id}")
    for m in messages[:4]:
        print(f"    [{m['role']:9}] {m['text'][:96]}")
    print("    ...")

    bio_facts: List[Dict] = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(account_id, limit=100)
    except Exception as e:
        print(f"  ⚠️  biographical context unavailable: {e}")

    results = []
    variants = [("baseline", False)] if rule == "baseline" else \
               [("new", True)] if rule == "new" else \
               [("baseline", False), ("new", True)]
    for label, use_new in variants:
        ops, elapsed, tokens, hit = await run_stage1(
            agent, messages, bio_facts, user_id, account_id, use_new,
        )
        if use_new and not hit:
            print("\n  ⚠️  classification block NOT matched — the rule text changed upstream. "
                  "Result below is the BASELINE rule, not the candidate.")
        report(f"RULE: {label}", ops, elapsed, tokens)
        results.append({"rule": label, "patched": hit, "elapsed_s": round(elapsed, 1),
                        "tokens": tokens, "operations": ops})

    out_dir = Path("scripts/memory/consolidation")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"stage1_classification_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({
        "model": agent.model_name,
        "window_turns": len(messages),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  full output → {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stage 1 directive/preference classification bench")
    ap.add_argument("--rule", choices=["baseline", "new", "both"], default="both")
    ap.add_argument("--messages", type=int, default=50, help="conversation turns to consolidate")
    ap.add_argument("--user-id", default=os.getenv("DEV_USER_ID"))
    ap.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"))
    a = ap.parse_args()
    if not a.user_id or not a.account_id:
        print("ERROR: set DEV_USER_ID / DEV_ACCOUNT_ID in .env")
        sys.exit(1)
    asyncio.run(main(a.rule, a.messages, a.user_id, a.account_id))
