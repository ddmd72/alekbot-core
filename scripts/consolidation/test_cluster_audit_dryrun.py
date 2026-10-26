#!/usr/bin/env python3
"""
ConsolidationAgent Dry-Run — Cluster Quality Audit
====================================================
Fetches top N long facts (word count > MIN_WORDS), processes them in batches of
BATCH_SIZE. For each batch the agent searches the knowledge base for each fact,
then analyses the combined cluster (input facts + all found results) for:
  - Duplications  — two facts covering the same information (candidates for MERGE)
  - Inconsistencies — facts that contradict each other (supersede the stale one)
  - Atomicity      — compound facts that should be decomposed

Fact *reads* (search_existing_facts) hit the real Firestore.
Fact *writes* (create/update/merge/discard) are intercepted — nothing is written.

Output per batch:
  reasoning_log  — expanded reasoning text per decision (why)
  operations     — action + fact text only (no metadata)

Usage:
    python scripts/consolidation/test_cluster_audit_dryrun.py
    python scripts/consolidation/test_cluster_audit_dryrun.py --limit 30 --batch-size 5 --min-words 40
    python scripts/consolidation/test_cluster_audit_dryrun.py --user-id <uid> --account-id <aid>
    python scripts/consolidation/test_cluster_audit_dryrun.py --s3-kit --limit 20 --batch-size 5

`--s3-kit` (Facts S3, plan T12b) runs each batch as a real `consolidate_cluster` pass (pass label and actor `cluster`) on
the dry-run kit: the cluster is built from the batch and its search results and sent explicitly, the writes go to the kit's
no-write store, and a report lands in `scripts/memory/`. Without the flag the bench behaves as it always did.

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

from google.cloud import firestore

from scripts.consolidation.legacy_dry_run_ports import install_dry_run
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.agents.consolidation_agent import ConsolidationAgent
from src.domain.entities import FactState
from src.composition.service_container import ServiceContainer
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.consolidation import consolidation_agent_id
from src.domain.request_context import RequestContext
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.domain.fact_write_context import FactWriteContext
from src.ports.fact_management_port import FactManagementPort
from src.composition.user_agent_factory import UserAgentFactory

from scripts.consolidation.s3_bench_support import (
    add_s3_arguments,
    biographical_facts,
    build_agent,
    chunks as batches_of,
    cluster_from_search,
    execute,
    kit_message,
    kit_session,
    load_env,
    print_rows,
)


# ─────────────────────────────────────────────────────────────────────────────
# Dry-Run Adapter
# ─────────────────────────────────────────────────────────────────────────────

class DryRunFactManagementAdapter(FactManagementPort):
    """Real reads, intercepted writes."""

    def __init__(self, real_port: FactManagementPort) -> None:
        self._real = real_port
        self.operations: List[Dict[str, Any]] = []

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20,
        *,
        timezone: str,
    ) -> List[Dict[str, Any]]:
        results = await self._real.search_existing_facts(
            keywords, primary_query, alternative_query, limit, timezone=timezone
        )
        print(f"    🔍 search({primary_query!r:.50}) → {len(results)} facts")
        return results

    async def create_fact(
        self, content: str, metadata: Dict[str, Any], *, context: FactWriteContext
    ) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append({"action": "CREATE", "fact_id": fake_id, "content": content, "metadata": metadata})
        print(f"    ✅ [DRY-RUN] CREATE: {content[:90]}")
        return {"fact_id": fake_id, "status": "created", "message": "[DRY-RUN] not written"}

    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any], account_id: str, *, context: FactWriteContext
    ) -> Dict[str, Any]:
        self.operations.append({"action": "UPDATE", "fact_id": fact_id, "updates": updates})
        preview = {k: v for k, v in updates.items() if "vector" not in k}
        print(f"    ✏️  [DRY-RUN] UPDATE {fact_id}: {preview}")
        return {"fact_id": fact_id, "status": "updated", "version": 99, "message": "[DRY-RUN] not written"}

    async def merge_facts(
        self, fact_ids: List[str], merged_content: str, metadata: Dict[str, Any], *, context: FactWriteContext
    ) -> Dict[str, Any]:
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
    if selected:
        print(f"  Longest: {selected[0]['word_count']} words  |  Shortest in selection: {selected[-1]['word_count']} words")
    return selected


def chunks(lst: List, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def build_system_alert(batch: List[Dict[str, Any]]) -> str:
    n = len(batch)
    return (
        f"SYSTEM MAINTENANCE — CLUSTER QUALITY AUDIT\n\n"
        f"You are given {n} existing facts for cross-cluster analysis. "
        f"For each fact: search the knowledge base, collect all relevant results. "
        f"Then analyse the COMBINED cluster (all {n} facts + everything you found) for:\n"
        f"  • Duplications — facts covering the same information → MERGE\n"
        f"  • Inconsistencies — facts that contradict each other → supersede the stale one via UPDATE\n"
        f"  • Atomicity — compound facts covering multiple distinct concepts → decompose\n\n"
        f"In your final report, for each decision provide an EXPANDED reason: "
        f"explain what specifically triggered it, which other facts it relates to, "
        f"and why this operation is the right choice.\n\n"
        f"Facts are existing records (not new candidates). "
        f"The original fact_id is provided in each entry.\n\n"
        f"Facts to audit:"
    )


def build_user_message(batch: List[Dict[str, Any]]) -> str:
    lines = [build_system_alert(batch), ""]
    for i, fact in enumerate(batch, 1):
        obj = {
            "fact_id": fact["fact_id"],
            "content": fact["content"],
            "word_count": fact["word_count"],
            "domain": fact["domain"],
            "tags": fact["tags"],
        }
        lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
    return "\n".join(lines)


def parse_agent_report(llm_turns: List[Dict]) -> List[Dict]:
    """Extract operations with reason from the final non-tool turn."""
    for turn in reversed(llm_turns):
        text = turn.get("text", "")
        if not text or turn.get("tool_calls"):
            continue
        for pattern in [
            r"```json\s*(\{.*?\})\s*```",
            r"(\{.*\"operations\".*\})",
        ]:
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
    """Keep only action + reason — the 'why' layer."""
    return [
        {
            "action": op.get("action"),
            "fact_id": op.get("fact_id"),
            "reason": op.get("reason", ""),
        }
        for op in agent_report
        if op.get("reason")
    ]


def extract_operations_slim(tool_calls_log: List[Dict]) -> List[Dict]:
    """Keep only action + fact text — strip all metadata."""
    slim = []
    for op in tool_calls_log:
        action = op.get("action")
        if action == "CREATE":
            slim.append({"action": "CREATE", "content": op.get("content", "")})
        elif action == "UPDATE":
            updates = op.get("updates", {})
            slim.append({
                "action": "UPDATE",
                "fact_id": op.get("fact_id"),
                "content": updates.get("content", ""),
                "state": updates.get("state", ""),
            })
        elif action == "MERGE":
            slim.append({
                "action": "MERGE",
                "old_ids": op.get("old_ids", []),
                "content": op.get("content", ""),
            })
        elif action == "DISCARD":
            slim.append({"action": "DISCARD", "reason": op.get("reason", "")})
    return slim


def print_batch_summary(batch_idx: int, reasoning: List[Dict], ops: List[Dict], elapsed: float) -> None:
    counts = Counter(op["action"] for op in ops)
    print(f"\n  Batch {batch_idx + 1}: {elapsed:.1f}s — ", end="")
    print("  ".join(f"{a}:{counts.get(a, 0)}" for a in ["CREATE", "UPDATE", "MERGE", "DISCARD"]))
    for r in reasoning:
        print(f"    [{r['action']}] {r['reason'][:120]}")


# ─────────────────────────────────────────────────────────────────────────────
# S3 kit mode: each batch is a real `consolidate_cluster` pass
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_long_facts_entities(repository, account_id: str, min_words: int, limit: int) -> List[Any]:
    """The stored facts longer than `min_words`, longest first (entities, so a cluster entry shows their decision)."""
    facts = await repository.get_active_facts(account_id)
    long_facts = sorted(
        (f for f in facts if len(f.text.split()) > min_words), key=lambda f: len(f.text.split()), reverse=True
    )
    print(f"Fetched {len(facts)} current facts; {len(long_facts)} with >{min_words} words → processing top {min(limit, len(long_facts))}")
    return long_facts[:limit]


def build_cluster_audit_message(
    kit, cluster: List[Dict[str, Any]], bio_facts: List[Dict], user_id: str, account_id: str, batch_idx: int = 0
):
    """The S3 bench's message: a `consolidate_cluster` pass over an explicit cluster (pass label and actor `cluster`)."""
    return kit_message(
        kit, sender="cluster_audit_script", user_id=user_id, account_id=account_id, task="consolidate_cluster",
        payload={"cluster": cluster, "biographical_context": bio_facts}, session_id=f"cluster_audit_batch{batch_idx}",
    )


async def build_batch_cluster(
    agent, batch: List[Any], timezone: str, user_id: str, account_id: str
) -> List[Dict[str, Any]]:
    """The batch's own entries, then what the knowledge-base search finds for each of the batch's facts.

    Each fact is read again through the kit's store first: an earlier batch's pass may have merged, superseded or
    rewritten it, and then the first read (before the kit) is stale. A fact that is no longer current is left out."""
    current: List[Any] = []
    for fact in batch:
        fresh = await agent._repo.get_fact_by_id(fact.id)
        if fresh is not None and fresh.state is FactState.CURRENT:
            current.append(fresh)
    if not current:
        return []
    seeds = [ConsolidationAgent._cluster_entry(fact, timezone) for fact in current]
    return await cluster_from_search(
        agent._fact_management.search_existing_facts, [f.text for f in current], seeds, timezone,
        user_id=user_id, account_id=account_id,
    )


async def run_s3_kit(handles, limit: int, batch_size: int, min_words: int, args) -> None:
    print(f"\n{'='*65}\nCLUSTER QUALITY AUDIT ON THE S3 KIT (consolidate_cluster passes)\n{'='*65}")
    print(f"  Min words: {min_words}  |  Limit: {limit}  |  Batch size: {batch_size}")
    user_id, account_id = args.user_id, args.account_id
    facts = await fetch_long_facts_entities(handles.container.repository, account_id, min_words, limit)
    if not facts:
        print(f"No facts found with >{min_words} words. Nothing to process.")
        return
    bio_facts = await biographical_facts(handles, account_id)

    async with kit_session(handles, args, user_id, account_id, "cluster_audit") as (kit, extra):
        extra.update({"model": handles.agent.model_name, "facts_analyzed": len(facts), "batches": []})
        for batch_idx, batch in enumerate(batches_of(facts, batch_size)):
            first_pass, first_row = len(kit.passes), len(kit.rows)
            cluster = await build_batch_cluster(handles.agent, list(batch), kit.timezone, user_id, account_id)
            if not cluster:
                print(f"\nBatch {batch_idx + 1}: none of its {len(batch)} facts is current any more; skipped.")
                extra["batches"].append({"batch_index": batch_idx, "skipped": True})
                continue
            print(f"\nBatch {batch_idx + 1}: {len(batch)} facts, cluster of {len(cluster)}")
            message = build_cluster_audit_message(kit, cluster, bio_facts, user_id, account_id, batch_idx)
            await execute(handles.agent, message, user_id, account_id, f"consolidate_cluster batch {batch_idx + 1}")
            extra["batches"].append({
                "batch_index": batch_idx, "input_fact_ids": [f.id for f in batch], "cluster_size": len(cluster),
                "passes": [first_pass, len(kit.passes)], "rows": [first_row, len(kit.rows)],
            })
            print_rows(kit.rows[first_row:])


async def main_s3_kit(limit: int, batch_size: int, min_words: int, args) -> None:
    await run_s3_kit(await build_agent(args.user_id), limit, batch_size, min_words, args)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def run_batch(
    consolidation_agent,
    batch: List[Dict[str, Any]],
    batch_idx: int,
    bio_facts: List[Dict],
    user_id: str,
    account_id: str,
    session_id: str,
) -> Tuple[List[Dict], List[Dict], List[Dict], float]:
    """Run one batch through the agent. Returns (reasoning_log, ops_slim, tool_calls_log, elapsed)."""
    # Fresh DryRun adapter per batch
    real_fm = consolidation_agent._fact_management
    if hasattr(real_fm, "_real"):
        real_fm = real_fm._real  # unwrap previous batch's adapter
    dry_run = DryRunFactManagementAdapter(real_fm)
    install_dry_run(consolidation_agent, dry_run)

    # Capture LLM turns
    llm_turns: List[Dict[str, Any]] = []
    _original_call_llm = consolidation_agent._call_llm

    async def _capturing_call_llm(request, turn=None):
        llm_response = await _original_call_llm(request, turn=turn)
        llm_turns.append({
            "turn": turn,
            "text": llm_response.text or "",
            "tool_calls": [{"name": tc.name, "args": tc.args} for tc in (llm_response.tool_calls or [])],
        })
        return llm_response

    consolidation_agent._call_llm = _capturing_call_llm

    user_message = build_user_message(batch)
    message = AgentMessage.create(
        sender="cluster_audit_script",
        recipient=consolidation_agent_id(user_id),
        intent=AgentIntent.DELEGATE,
        payload={
            "messages": [{"role": "user", "text": user_message, "timestamp": time.time()}],
            "biographical_context": bio_facts,
        },
        context={
            "user_id": user_id,
            "account_id": account_id,
            "session_id": f"{session_id}_batch{batch_idx}",
            "routing": {"user_tone": "system", "semantic_lens": ["biographical", "maintenance"], "confidence": 1.0},
        },
    )

    print(f"\n{'─'*55}")
    print(f"Batch {batch_idx + 1}: {len(batch)} facts")
    for f in batch:
        print(f"  [{f['word_count']}w] {f['content'][:70]}...")

    t0 = time.time()
    async with RequestContext(user_id=user_id, account_id=account_id):
        await consolidation_agent.execute(message)
    elapsed = time.time() - t0

    # Restore original _call_llm for next batch
    consolidation_agent._call_llm = _original_call_llm

    agent_report = parse_agent_report(llm_turns)
    reasoning_log = extract_reasoning_log(agent_report)
    ops_slim = extract_operations_slim(dry_run.operations)

    return reasoning_log, ops_slim, dry_run.operations, elapsed


async def main(limit: int, batch_size: int, min_words: int, user_id: str, account_id: str) -> None:
    print(f"\n{'='*65}")
    print("CLUSTER QUALITY AUDIT — DRY-RUN")
    print(f"{'='*65}")
    print(f"  Min words: {min_words}  |  Limit: {limit}  |  Batch size: {batch_size}")
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

    # ── Fetch facts ───────────────────────────────────────────────────
    facts = await fetch_long_facts(container.repository, account_id, min_words, limit)
    if not facts:
        print(f"No facts found with >{min_words} words. Nothing to process.")
        return

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

    # Store original _fact_management for batch unwrapping
    consolidation_agent._fact_management = consolidation_agent._fact_management  # no-op, just for clarity
    if consolidation_agent._fact_management is None:
        print("ERROR: _fact_management is None.")
        return

    # Suppress post-processing side effects
    async def _noop_refresh(*args, **kwargs):
        pass
    def _noop_invalidate(*args, **kwargs):
        pass
    consolidation_agent._repo.refresh_biographical_context_cache = _noop_refresh
    if consolidation_agent.prompt_builder:
        consolidation_agent.prompt_builder.invalidate_biographical_cache = _noop_invalidate

    # ── Biographical context ──────────────────────────────────────────
    bio_facts = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(account_id, limit=100)
        print(f"Loaded {len(bio_facts)} biographical facts for context.")
    except Exception as e:
        print(f"⚠️  Could not load biographical facts: {e}")

    session_id = "cluster_audit"
    try:
        session_id = await container.session_store.get_latest_session_id(user_id) or session_id
    except Exception:
        pass

    # ── Batch loop ────────────────────────────────────────────────────
    all_batches = []
    total_counts = Counter()
    t_total = time.time()

    for batch_idx, batch in enumerate(chunks(facts, batch_size)):
        try:
            reasoning_log, ops_slim, tool_calls_raw, elapsed = await run_batch(
                consolidation_agent, batch, batch_idx, bio_facts, user_id, account_id, session_id
            )
        except Exception as e:
            print(f"\n❌ Batch {batch_idx + 1} error: {e}")
            raise

        total_counts.update(op["action"] for op in ops_slim)
        print_batch_summary(batch_idx, reasoning_log, ops_slim, elapsed)

        all_batches.append({
            "batch_index": batch_idx,
            "input_fact_ids": [f["fact_id"] for f in batch],
            "elapsed_s": round(elapsed, 1),
            "reasoning_log": reasoning_log,
            "operations": ops_slim,
        })

    total_elapsed = time.time() - t_total

    # ── Final summary ─────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print("CLUSTER AUDIT COMPLETE")
    print(f"{'='*65}")
    print(f"  Batches: {len(all_batches)}  |  Total elapsed: {total_elapsed:.1f}s")
    print(f"  Total operations: {sum(total_counts.values())}")
    for action in ["CREATE", "UPDATE", "MERGE", "DISCARD"]:
        n = total_counts.get(action, 0)
        if n:
            print(f"    {action}: {n}")

    # ── Save ──────────────────────────────────────────────────────────
    out_dir = Path("scripts/memory/consolidation")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"cluster_audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_file.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "meta": {
                    "facts_analyzed": len(facts),
                    "min_words": min_words,
                    "batch_size": batch_size,
                    "batches_run": len(all_batches),
                    "longest_fact_words": facts[0]["word_count"] if facts else 0,
                },
                "total_elapsed_s": round(total_elapsed, 1),
                "summary": {a: total_counts.get(a, 0) for a in ["CREATE", "UPDATE", "MERGE", "DISCARD"]},
                "batches": all_batches,
                "input_facts": facts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nFull results → {out_file}")


if __name__ == "__main__":
    load_env()
    parser = argparse.ArgumentParser(description="ConsolidationAgent dry-run: cluster quality audit")
    parser.add_argument("--limit", type=int, default=30, help="Total facts to process (default: 30)")
    parser.add_argument("--batch-size", type=int, default=5, help="Facts per batch (default: 5)")
    parser.add_argument("--min-words", type=int, default=40, help="Minimum word count threshold (default: 40)")
    parser.add_argument(
        "--user-id",
        default=os.getenv("DEV_USER_ID") or os.getenv("TEST_USER_ID"),
        help="User ID (defaults to DEV_USER_ID from .env)",
    )
    parser.add_argument(
        "--account-id",
        default=os.getenv("DEV_ACCOUNT_ID") or os.getenv("TEST_ACCOUNT_ID"),
        help="Account ID (defaults to DEV_ACCOUNT_ID from .env)",
    )
    add_s3_arguments(parser)
    args = parser.parse_args()

    if not args.user_id:
        print("ERROR: user_id required. Set DEV_USER_ID in .env or pass --user-id")
        sys.exit(1)
    if not args.account_id:
        print("ERROR: account_id required. Set DEV_ACCOUNT_ID in .env or pass --account-id")
        sys.exit(1)

    if args.s3_kit:
        asyncio.run(main_s3_kit(args.limit, args.batch_size, args.min_words, args))
    else:
        asyncio.run(main(
            limit=args.limit,
            batch_size=args.batch_size,
            min_words=args.min_words,
            user_id=args.user_id,
            account_id=args.account_id,
        ))
