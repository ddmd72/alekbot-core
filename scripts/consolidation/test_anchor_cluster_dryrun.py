#!/usr/bin/env python3
"""
ConsolidationAgent Dry-Run — Anchor + Pre-fetched Cluster (Variant C)
======================================================================
For each anchor fact (long fact from the DB):
  1. Python pre-fetches its cluster: top CLUSTER_SIZE similar facts from Firestore.
  2. Agent receives: anchor fact + full cluster already injected in the message.
  3. Agent analyses the cluster holistically (duplications, inconsistencies, atomicity).
  4. Agent does NOT search again — DryRunAdapter returns pre-fetched results directly.

Fact *writes* (create/update/merge) are intercepted — nothing is written.

Usage:
    python scripts/consolidation/test_anchor_cluster_dryrun.py
    python scripts/consolidation/test_anchor_cluster_dryrun.py --limit 5 --cluster-size 20
    python scripts/consolidation/test_anchor_cluster_dryrun.py --user-id <uid> --account-id <aid>

Pre-conditions:
    .env must have: DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE, GEMINI_API_KEY
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

from google.cloud import firestore

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.composition.service_container import ServiceContainer
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.request_context import RequestContext
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.ports.fact_management_port import FactManagementPort
from src.composition.user_agent_factory import UserAgentFactory


# ─────────────────────────────────────────────────────────────────────────────
# Cluster-Aware Dry-Run Adapter
# search_existing_facts → returns pre-fetched cluster (no extra Firestore calls)
# writes → intercepted, logged, NOT written
# ─────────────────────────────────────────────────────────────────────────────

class ClusterDryRunAdapter(FactManagementPort):
    """
    Dry-run adapter with pre-loaded cluster.
    All search calls return the pre-fetched cluster regardless of query —
    the agent works with exactly the data we showed it in the message.
    """

    def __init__(self, real_port: FactManagementPort, cluster: List[Dict[str, Any]]) -> None:
        self._real = real_port
        self._cluster = cluster
        self.operations: List[Dict[str, Any]] = []

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        # Return pre-fetched cluster — no Firestore call
        results = self._cluster[:limit]
        print(f"    🔍 [CLUSTER] search({primary_query!r:.50}) → {len(results)} pre-fetched facts")
        return results

    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append({"action": "CREATE", "fact_id": fake_id, "content": content, "metadata": metadata})
        print(f"    ✅ [DRY-RUN] CREATE: {content[:90]}")
        return {"fact_id": fake_id, "status": "created", "message": "[DRY-RUN] not written"}

    async def update_fact(self, fact_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        self.operations.append({"action": "UPDATE", "fact_id": fact_id, "updates": updates})
        preview = {k: v for k, v in updates.items() if "vector" not in k}
        print(f"    ✏️  [DRY-RUN] UPDATE {fact_id}: {preview}")
        return {"fact_id": fact_id, "status": "updated", "version": 99, "message": "[DRY-RUN] not written"}

    async def merge_facts(self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append({"action": "MERGE", "fact_id": fake_id, "old_ids": fact_ids, "content": merged_content})
        print(f"    🔀 [DRY-RUN] MERGE {fact_ids} → {merged_content[:80]}")
        return {"new_fact_id": fake_id, "old_fact_ids": fact_ids, "status": "merged", "message": "[DRY-RUN] not written"}

    async def discard_candidate(self, reason: str) -> Dict[str, Any]:
        self.operations.append({"action": "DISCARD", "reason": reason})
        print(f"    🗑️  [DRY-RUN] DISCARD: {reason[:100]}")
        return {"status": "discarded", "message": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_long_facts(repository, account_id: str, min_words: int, limit: int) -> List[Dict[str, Any]]:
    facts = await repository.get_active_facts(account_id)
    print(f"Fetched {len(facts)} current facts from Firestore.")
    candidates = []
    for f in facts:
        wc = len(f.text.split())
        if wc > min_words:
            candidates.append({
                "fact_id": f.id,
                "content": f.text,
                "word_count": wc,
                "domain": f.domain.value if f.domain else None,
                "tags": f.tags or [],
            })
    candidates.sort(key=lambda x: x["word_count"], reverse=True)
    selected = candidates[:limit]
    print(f"Facts with >{min_words} words: {len(candidates)} → processing top {len(selected)}")
    return selected


async def prefetch_cluster(
    fact_management_port: FactManagementPort,
    anchor: Dict[str, Any],
    cluster_size: int,
) -> List[Dict[str, Any]]:
    """Fetch top cluster_size similar facts for the anchor, excluding anchor itself."""
    results = await fact_management_port.search_existing_facts(
        keywords=anchor["tags"][:10],
        primary_query=anchor["content"][:300],
        alternative_query=f"{anchor['domain']} {' '.join(anchor['tags'][:5])}",
        limit=cluster_size + 1,  # +1 to account for possible self-inclusion
    )
    return [r for r in results if r.get("fact_id") != anchor["fact_id"]][:cluster_size]


def build_system_alert(anchor: Dict[str, Any], cluster: List[Dict[str, Any]]) -> str:
    return (
        "SYSTEM MAINTENANCE — FACT CLUSTER REVIEW\n\n"
        "The system has flagged the following cluster of facts for quality review.\n"
        "This cluster may contain: repeated or overlapping facts (these must be merged),\n"
        "facts that span multiple distinct concepts (these must be decomposed, with the\n"
        "original superseded), mutually inconsistent facts, or facts that have grown\n"
        "too large to serve as atomic memory units.\n\n"
        "Review and refactor this cluster according to your consolidation rules.\n"
        "When creating new facts, ensure they do not duplicate information already\n"
        "present in other facts in this cluster.\n\n"
        "Hard limit: no fact may exceed 40 words. Every fact in this cluster that\n"
        "exceeds 40 words must be either rephrased to fit within 40 words, or\n"
        "decomposed into atomic facts each under 40 words. Co-location is not a\n"
        "valid justification for exceeding this limit.\n\n"
        "Important: do not lose specific numeric values, dates, or amounts —\n"
        "they are critical for long-term memory accuracy."
    )


def build_user_message(anchor: Dict[str, Any], cluster: List[Dict[str, Any]]) -> str:
    lines = [build_system_alert(anchor, cluster), "", ""]
    # Anchor is fact #1, cluster facts follow — agent doesn't know which is the anchor
    all_facts = [anchor] + cluster
    for i, fact in enumerate(all_facts, 1):
        obj = {
            "fact_id": fact.get("fact_id"),
            "content": fact.get("content"),
            "similarity": round(fact.get("similarity") or 0, 3) if fact.get("similarity") is not None else None,
            "source": fact.get("source"),
        }
        lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
    return "\n".join(lines)


def parse_agent_report(llm_turns: List[Dict]) -> List[Dict]:
    for turn in reversed(llm_turns):
        text = turn.get("text", "")
        if not text or turn.get("tool_calls"):
            continue
        for pattern in [r"```json\s*(\{.*?\})\s*```", r"(\{.*\"operations\".*\})"]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(1)).get("operations", [])
                except json.JSONDecodeError:
                    pass
        try:
            return json.loads(text).get("operations", [])
        except json.JSONDecodeError:
            pass
    return []


def extract_reasoning_log(agent_report: List[Dict]) -> List[Dict]:
    return [
        {"action": op.get("action"), "fact_id": op.get("fact_id"), "reason": op.get("reason", "")}
        for op in agent_report if op.get("reason")
    ]


def extract_operations_slim(tool_calls_log: List[Dict]) -> List[Dict]:
    slim = []
    for op in tool_calls_log:
        action = op.get("action")
        if action == "CREATE":
            slim.append({"action": "CREATE", "content": op.get("content", "")})
        elif action == "UPDATE":
            updates = op.get("updates", {})
            slim.append({"action": "UPDATE", "fact_id": op.get("fact_id"), "content": updates.get("content", ""), "state": updates.get("state", "")})
        elif action == "MERGE":
            slim.append({"action": "MERGE", "old_ids": op.get("old_ids", []), "content": op.get("content", "")})
        elif action == "DISCARD":
            slim.append({"action": "DISCARD", "reason": op.get("reason", "")})
    return slim


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run_anchor(
    consolidation_agent,
    real_fm: FactManagementPort,
    anchor: Dict[str, Any],
    anchor_idx: int,
    cluster: List[Dict[str, Any]],
    bio_facts: List[Dict],
    user_id: str,
    account_id: str,
    session_id: str,
    original_call_llm,
) -> Tuple[List[Dict], List[Dict], float]:
    """Run one anchor + cluster through the agent."""
    dry_run = ClusterDryRunAdapter(real_fm, cluster)
    consolidation_agent._fact_management = dry_run

    llm_turns: List[Dict[str, Any]] = []

    async def _capturing_call_llm(request, turn=None):
        llm_response = await original_call_llm(request, turn=turn)
        llm_turns.append({
            "turn": turn,
            "text": llm_response.text or "",
            "tool_calls": [{"name": tc.name, "args": tc.args} for tc in (llm_response.tool_calls or [])],
        })
        return llm_response

    consolidation_agent._call_llm = _capturing_call_llm

    user_message = build_user_message(anchor, cluster)

    message = AgentMessage.create(
        sender="anchor_cluster_script",
        recipient=f"consolidation_agent_{user_id}",
        intent=AgentIntent.DELEGATE,
        payload={
            "messages": [{"role": "user", "text": user_message, "timestamp": time.time()}],
            "biographical_context": bio_facts,
        },
        context={
            "user_id": user_id,
            "account_id": account_id,
            "session_id": f"{session_id}_anchor{anchor_idx}",
            "routing": {"user_tone": "system", "semantic_lens": ["biographical", "maintenance"], "confidence": 1.0},
        },
    )

    t0 = time.time()
    async with RequestContext(user_id=user_id, account_id=account_id):
        await consolidation_agent.execute(message)
    elapsed = time.time() - t0

    # Restore original call_llm
    consolidation_agent._call_llm = original_call_llm

    agent_report = parse_agent_report(llm_turns)
    reasoning_log = extract_reasoning_log(agent_report)
    ops_slim = extract_operations_slim(dry_run.operations)

    return reasoning_log, ops_slim, elapsed


async def main(limit: int, cluster_size: int, min_words: int, user_id: str, account_id: str, no_bio: bool = False) -> None:
    print(f"\n{'='*65}")
    print("ANCHOR CLUSTER ANALYSIS — DRY-RUN (Variant C)")
    print(f"{'='*65}")
    print(f"  Min words: {min_words}  |  Anchors: {limit}  |  Cluster size: {cluster_size}")
    print(f"  Bio in prompt: {'DISABLED (--no-bio)' if no_bio else 'enabled'}")
    print(f"  User: {user_id}")

    # ── Infrastructure ────────────────────────────────────────────────
    database_id = os.getenv("FIRESTORE_DATABASE", "us-production")
    db = firestore.AsyncClient(database=database_id)
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]

    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    coordinator = AgentCoordinator()
    container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=account_repo)

    # ── Factory + agents ──────────────────────────────────────────────
    factory = UserAgentFactory(
        config=config, env_config=env_config, coordinator=coordinator,
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    print("\nCreating agents...")
    agents = await factory.ensure_agents_for_user(user_id)
    consolidation_agent = agents.get("consolidation_agent")
    if not consolidation_agent:
        print(f"consolidation_agent not found. Keys: {list(agents.keys())}")
        return

    real_fm = consolidation_agent._fact_management
    if real_fm is None:
        print("ERROR: _fact_management is None.")
        return

    # Suppress post-processing side effects
    async def _noop(*args, **kwargs): pass
    consolidation_agent._repo.refresh_biographical_context_cache = _noop
    if consolidation_agent.prompt_builder:
        consolidation_agent.prompt_builder.invalidate_biographical_cache = lambda *a, **k: None

    # --no-bio: patch build_for_agent to force include_biographical=False
    # This removes bio facts from the static prompt (the real 80k token block).
    if no_bio and consolidation_agent.prompt_builder:
        _original_build = consolidation_agent.prompt_builder.build_for_agent
        async def _build_no_bio(*args, **kwargs):
            kwargs["include_biographical"] = False
            kwargs.pop("biographical_facts", None)
            return await _original_build(*args, **kwargs)
        consolidation_agent.prompt_builder.build_for_agent = _build_no_bio
        print("⚠️  Bio context DISABLED in prompt (include_biographical=False)")

    # Store original _call_llm once — restored after each anchor
    original_call_llm = consolidation_agent._call_llm

    # ── Biographical context ──────────────────────────────────────────
    # Note: bio facts are baked into the static prompt by PromptBuilder —
    # passing them here only saves one extra Firestore call inside the agent.
    bio_facts = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(account_id, limit=100)
        print(f"Loaded {len(bio_facts)} biographical facts for context.")
    except Exception as e:
        print(f"⚠️  Could not load biographical facts: {e}")

    session_id = "anchor_cluster_audit"
    try:
        session_id = await container.session_store.get_latest_session_id(user_id) or session_id
    except Exception:
        pass

    # ── Anchor loop ───────────────────────────────────────────────────
    # Each round re-fetches the fact list from DB — mirrors production behavior
    # where a processed anchor would already be SUPERSEDED and not appear in the
    # next round's selection. In dry-run, writes are intercepted, so we track
    # processed ids manually to avoid re-selecting the same anchor.
    all_anchors_out = []
    total_counts = Counter()
    t_total = time.time()
    processed_ids: set = set()

    for anchor_idx in range(limit):
        # Re-fetch each round — in production the DB state would have changed
        candidates = await fetch_long_facts(container.repository, account_id, min_words, limit=limit * 3)
        anchor = next((f for f in candidates if f["fact_id"] not in processed_ids), None)
        if not anchor:
            print(f"\nNo more eligible anchors after {anchor_idx} rounds.")
            break
        processed_ids.add(anchor["fact_id"])

        print(f"\n{'─'*65}")
        print(f"Round {anchor_idx + 1}/{limit}: [{anchor['word_count']}w] {anchor['content'][:80]}...")

        # Pre-fetch cluster (needs RequestContext for SearchEnrichmentService)
        async with RequestContext(user_id=user_id, account_id=account_id):
            cluster = await prefetch_cluster(real_fm, anchor, cluster_size)
        print(f"  Cluster: {len(cluster)} facts pre-fetched")
        for f in cluster[:3]:
            sim = f.get("similarity")
            sim_str = f"{sim:.3f}" if sim is not None else "N/A"
            print(f"    [{sim_str}] {f.get('content', '')[:70]}...")

        try:
            reasoning_log, ops_slim, elapsed = await run_anchor(
                consolidation_agent, real_fm, anchor, anchor_idx,
                cluster, bio_facts, user_id, account_id, session_id, original_call_llm,
            )
        except Exception as e:
            print(f"\n❌ Anchor {anchor_idx + 1} error: {e}")
            raise

        counts = Counter(op["action"] for op in ops_slim)
        total_counts.update(counts)
        print(f"\n  → {elapsed:.1f}s | " + "  ".join(f"{a}:{counts.get(a,0)}" for a in ["CREATE","UPDATE","MERGE","DISCARD"]))
        for r in reasoning_log:
            print(f"    [{r['action']}] {r['reason'][:120]}")

        all_anchors_out.append({
            "anchor_idx": anchor_idx,
            "anchor_fact_id": anchor["fact_id"],
            "anchor_content": anchor["content"],
            "cluster_size": len(cluster),
            "elapsed_s": round(elapsed, 1),
            "reasoning_log": reasoning_log,
            "operations": ops_slim,
        })

    total_elapsed = time.time() - t_total

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("ANCHOR CLUSTER AUDIT COMPLETE")
    print(f"{'='*65}")
    print(f"  Rounds completed: {len(all_anchors_out)}  |  Total: {total_elapsed:.1f}s")
    for action in ["CREATE", "UPDATE", "MERGE", "DISCARD"]:
        n = total_counts.get(action, 0)
        if n:
            print(f"    {action}: {n}")

    # ── Save ──────────────────────────────────────────────────────────
    out_dir = Path("scripts/memory/consolidation")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_nobio" if no_bio else ""
    out_file = out_dir / f"anchor_cluster_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}.json"
    out_file.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "meta": {
                    "anchors_analyzed": len(all_anchors_out),
                    "cluster_size": cluster_size,
                    "min_words": min_words,
                    "bio_in_prompt": not no_bio,
                },
                "total_elapsed_s": round(total_elapsed, 1),
                "summary": {a: total_counts.get(a, 0) for a in ["CREATE", "UPDATE", "MERGE", "DISCARD"]},
                "anchors": all_anchors_out,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results → {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ConsolidationAgent dry-run: anchor + pre-fetched cluster")
    parser.add_argument("--limit", type=int, default=5, help="Number of anchor facts to process (default: 5)")
    parser.add_argument("--cluster-size", type=int, default=20, help="Similar facts per anchor (default: 20)")
    parser.add_argument("--min-words", type=int, default=40, help="Min word count for anchor facts (default: 40)")
    parser.add_argument("--user-id", default=os.getenv("DEV_USER_ID") or os.getenv("TEST_USER_ID"))
    parser.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID") or os.getenv("TEST_ACCOUNT_ID"))
    parser.add_argument("--no-bio", action="store_true", help="Remove biographical context from prompt (include_biographical=False)")
    args = parser.parse_args()

    if not args.user_id:
        print("ERROR: user_id required. Set DEV_USER_ID in .env or pass --user-id")
        sys.exit(1)
    if not args.account_id:
        print("ERROR: account_id required. Set DEV_ACCOUNT_ID in .env or pass --account-id")
        sys.exit(1)

    asyncio.run(main(
        limit=args.limit,
        cluster_size=args.cluster_size,
        min_words=args.min_words,
        user_id=args.user_id,
        account_id=args.account_id,
        no_bio=args.no_bio,
    ))
