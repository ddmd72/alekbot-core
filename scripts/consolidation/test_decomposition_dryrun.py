#!/usr/bin/env python3
"""
ConsolidationAgent Dry-Run — Size-Based Decomposition Review
=============================================================
Fetches all current facts from Firestore, filters those with word count > MIN_WORDS,
sorts by descending word count (longest first), and feeds the top N to ConsolidationAgent
with a system maintenance alert requesting decomposition analysis.

Fact *reads* (search_existing_facts) hit the real Firestore.
Fact *writes* (create/update/merge/discard) are intercepted and logged — nothing is written.

Usage:
    python scripts/consolidation/test_decomposition_dryrun.py
    python scripts/consolidation/test_decomposition_dryrun.py --limit 30 --min-words 40
    python scripts/consolidation/test_decomposition_dryrun.py --user-id <uid> --account-id <aid>

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
from typing import Any, Dict, List, Optional

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
# Dry-Run Adapters
# ─────────────────────────────────────────────────────────────────────────────

class ClusterDryRunAdapter(FactManagementPort):
    """
    Pre-loaded cluster adapter (used in --message mode).
    search_existing_facts → returns pre-fetched cluster, no Firestore calls.
    Writes → intercepted, logged, NOT written.
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
        results = self._cluster[:limit]
        print(f"    🔍 [CLUSTER] search({primary_query!r:.50}) → {len(results)} pre-fetched facts")
        return results

    async def create_fact(self, content: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append({"action": "CREATE", "fact_id": fake_id, "content": content, "metadata": metadata})
        domain = metadata.get("domain", "?")
        temporal = metadata.get("temporal_class", "?")
        print(f"    ✅ [DRY-RUN] CREATE [{domain}|{temporal}]: {content[:90]}")
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


class DryRunFactManagementAdapter(FactManagementPort):
    """
    Wraps a real FactManagementAdapter.
      - search_existing_facts  → real Firestore reads (dedup/conflict detection works)
      - create / update / merge / discard → intercepted, logged, NOT written
    """

    def __init__(self, real_port: FactManagementPort) -> None:
        self._real = real_port
        self.operations: List[Dict[str, Any]] = []

    async def search_existing_facts(
        self,
        keywords: List[str],
        primary_query: str,
        alternative_query: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        results = await self._real.search_existing_facts(
            keywords, primary_query, alternative_query, limit
        )
        print(f"    🔍 search({primary_query!r}) → {len(results)} existing facts")
        return results

    async def create_fact(
        self, content: str, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append(
            {"action": "CREATE", "fact_id": fake_id, "content": content, "metadata": metadata}
        )
        domain = metadata.get("domain", "?")
        temporal = metadata.get("temporal_class", "?")
        print(f"    ✅ [DRY-RUN] CREATE [{domain}|{temporal}]: {content[:90]}")
        return {"fact_id": fake_id, "status": "created", "message": "[DRY-RUN] not written"}

    async def update_fact(
        self, fact_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.operations.append(
            {"action": "UPDATE", "fact_id": fact_id, "updates": updates}
        )
        preview = {k: v for k, v in updates.items() if "vector" not in k}
        print(f"    ✏️  [DRY-RUN] UPDATE {fact_id}: {preview}")
        return {"fact_id": fact_id, "status": "updated", "version": 99, "message": "[DRY-RUN] not written"}

    async def merge_facts(
        self,
        fact_ids: List[str],
        merged_content: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        fake_id = f"dryrun_{uuid.uuid4().hex[:8]}"
        self.operations.append(
            {"action": "MERGE", "fact_id": fake_id, "old_ids": fact_ids, "content": merged_content}
        )
        print(f"    🔀 [DRY-RUN] MERGE {fact_ids} → {merged_content[:80]}")
        return {
            "new_fact_id": fake_id,
            "old_fact_ids": fact_ids,
            "status": "merged",
            "message": "[DRY-RUN] not written",
        }

    async def discard_candidate(self, reason: str) -> Dict[str, Any]:
        self.operations.append({"action": "DISCARD", "reason": reason})
        print(f"    🗑️  [DRY-RUN] DISCARD: {reason[:100]}")
        return {"status": "discarded", "message": reason}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_long_facts(
    repository,
    account_id: str,
    min_words: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Fetch all current facts from Firestore, filter by word count, return top N longest.
    Returns plain dicts (not FactEntity) ready for message formatting.
    """
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


def build_system_alert(n: int, min_words: int) -> str:
    return (
        f"SYSTEM MAINTENANCE — SIZE REVIEW\n\n"
        f"The system has identified {n} facts in the knowledge base with word count "
        f"exceeding {min_words} words. For each fact: search the knowledge base, "
        f"then triage and decompose based on what you find.\n\n"
        f"Facts are existing records (not new candidates). "
        f"The original fact_id is provided in each entry.\n\n"
        f"Facts to review:"
    )


def build_user_message(facts: List[Dict[str, Any]]) -> str:
    header = build_system_alert(len(facts), min_words=facts[-1]["word_count"] if facts else 0)
    lines = [header, ""]
    for i, fact in enumerate(facts, 1):
        obj = {
            "fact_id": fact["fact_id"],
            "content": fact["content"],
            "word_count": fact["word_count"],
            "domain": fact["domain"],
            "tags": fact["tags"],
        }
        lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
    return "\n".join(lines)


async def prefetch_cluster_from_operations(
    real_fm: FactManagementPort,
    operations: List[Dict[str, Any]],
    cluster_limit: int = 30,
) -> List[Dict[str, Any]]:
    """
    For each CREATE/UPDATE operation from stage 1, run search_existing_facts.
    Merge all results, deduplicate by fact_id, return top cluster_limit by similarity.
    """
    async def _search_one(content: str) -> List[Dict]:
        keywords = [w for w in content.split()[:10] if len(w) > 3]
        return await real_fm.search_existing_facts(
            keywords=keywords,
            primary_query=content,
            alternative_query="",
            limit=20,
        )

    contents = []
    for op in operations:
        if op.get("action") == "CREATE":
            contents.append(op.get("content", ""))
        elif op.get("action") == "UPDATE":
            contents.append(op.get("updates", {}).get("content", ""))
    contents = [c for c in contents if c]

    if not contents:
        print("  (no CREATE/UPDATE operations to search from)")
        return []

    print(f"\n--- Stage 2: pre-fetching cluster ({len(contents)} searches) ---")
    all_results = await asyncio.gather(*[_search_one(c) for c in contents])

    seen: Dict[str, Any] = {}
    for results in all_results:
        for fact in results:
            fid = fact.get("fact_id")
            if not fid:
                continue
            if fid not in seen or (fact.get("similarity") or 0) > (seen[fid].get("similarity") or 0):
                seen[fid] = fact

    merged = sorted(seen.values(), key=lambda f: f.get("similarity") or 0, reverse=True)
    result = merged[:cluster_limit]
    total = sum(len(r) for r in all_results)
    print(f"  Cluster: {len(result)} unique facts (from {total} total results across {len(contents)} searches)")
    return result


def build_cluster_message(cluster: List[Dict[str, Any]]) -> str:
    """RFC-validated system alert (WHAT not HOW) + numbered cluster list."""
    alert = (
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
    lines = [alert, "", ""]
    for i, fact in enumerate(cluster, 1):
        obj = {
            "fact_id": fact.get("fact_id"),
            "content": fact.get("content"),
            "similarity": round(fact.get("similarity") or 0, 3) if fact.get("similarity") is not None else None,
        }
        lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
    return "\n".join(lines)


def print_summary(operations: List[Dict], elapsed: float, facts_count: int) -> None:
    counts = Counter(op["action"] for op in operations)
    print("\n" + "=" * 65)
    print("DRY-RUN SUMMARY — DECOMPOSITION REVIEW")
    print("=" * 65)
    print(f"  Facts analyzed: {facts_count}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Operations: {len(operations)}")
    for action in ["CREATE", "UPDATE", "MERGE", "DISCARD"]:
        n = counts.get(action, 0)
        if n:
            print(f"    {action}: {n}")

    if not operations:
        print("  (no operations — agent decided all facts are already atomic)")
        return

    print("\nDetail:")
    for op in operations:
        action = op["action"]
        if action == "CREATE":
            m = op.get("metadata", {})
            domain = m.get("domain", "?")
            temporal = m.get("temporal_class", "?")
            print(f"  CREATE [{domain}|{temporal}]  {op['content'][:80]}")
        elif action == "UPDATE":
            keys = [k for k in op.get("updates", {}) if "vector" not in k]
            print(f"  UPDATE {op['fact_id']}  fields={keys}")
        elif action == "MERGE":
            print(f"  MERGE {op['old_ids']} → {op['content'][:70]}")
        elif action == "DISCARD":
            print(f"  DISCARD: {op['reason'][:90]}")


def parse_agent_report(llm_turns: List[Dict]) -> List[Dict]:
    """Extract agent_report (with reason fields) from the final non-tool turn."""
    for turn in reversed(llm_turns):
        text = turn.get("text", "")
        if not text or turn.get("tool_calls"):
            continue
        # Try markdown block
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1)).get("operations", [])
            except json.JSONDecodeError:
                pass
        # Try raw JSON
        try:
            return json.loads(text).get("operations", [])
        except json.JSONDecodeError:
            pass
        # Try embedded object
        m = re.search(r"\{.*\"operations\".*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0)).get("operations", [])
            except json.JSONDecodeError:
                pass
    return []


def save_results(
    facts: List[Dict],
    operations: List[Dict],
    llm_turns: List[Dict],
    elapsed: float,
    min_words: int,
) -> Path:
    out_dir = Path("scripts/memory/consolidation")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"decomposition_dryrun_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    agent_report = parse_agent_report(llm_turns)
    counts = Counter(op["action"] for op in operations)
    out_file.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "meta": {
                    "facts_analyzed": len(facts),
                    "min_words": min_words,
                    "longest_fact_words": facts[0].get("word_count", 0) if facts else 0,
                },
                "elapsed_s": round(elapsed, 1),
                "summary": {action: counts.get(action, 0) for action in ["CREATE", "UPDATE", "MERGE", "DISCARD"]},
                "agent_report": agent_report,
                "tool_calls_log": operations,
                "llm_turns": llm_turns,
                "input_facts": facts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_file


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main(
    limit: int,
    min_words: int,
    user_id: str,
    account_id: str,
    raw_message: Optional[str] = None,
) -> None:
    print(f"\n{'='*65}")
    print("FACT DECOMPOSITION DRY-RUN")
    print(f"{'='*65}")
    if raw_message:
        print("  Mode: RAW MESSAGE")
    else:
        print(f"  Min words: {min_words}  |  Limit: {limit}")
    print(f"  User: {user_id}")

    # ── Infrastructure ────────────────────────────────────────────────
    database_id = os.getenv("FIRESTORE_DATABASE", "us-production")
    db = firestore.AsyncClient(database=database_id)
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]

    account_repo = FirestoreAccountRepository(
        db_client=db,
        collection_name=env_config.account_collection_name,
    )
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    coordinator = AgentCoordinator()
    container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=account_repo)

    # ── Factory + agents ──────────────────────────────────────────────
    factory = UserAgentFactory(
        config=config,
        env_config=env_config,
        coordinator=coordinator,
        user_repo=user_repo,
        account_repo=account_repo,
        **container.agent_services(),
    )

    print("Creating agents...")
    agents = await factory.ensure_agents_for_user(user_id)
    consolidation_agent = agents.get("consolidation_agent")
    if not consolidation_agent:
        print(f"consolidation_agent not found. Keys: {list(agents.keys())}")
        return

    real_fm = consolidation_agent._fact_management
    if real_fm is None:
        print("ERROR: _fact_management is None — agent was built without FactManagementPort.")
        return

    # Suppress post-processing side effects
    async def _noop_refresh(*args, **kwargs):
        print("    [DRY-RUN] Skipping refresh_biographical_context_cache")
    def _noop_invalidate(*args, **kwargs):
        print("    [DRY-RUN] Skipping invalidate_biographical_cache")
    consolidation_agent._repo.refresh_biographical_context_cache = _noop_refresh
    if consolidation_agent.prompt_builder:
        consolidation_agent.prompt_builder.invalidate_biographical_cache = _noop_invalidate

    # ── Biographical context ──────────────────────────────────────────
    bio_facts = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(
            account_id, limit=100
        )
        print(f"Loaded {len(bio_facts)} biographical facts for context.")
    except Exception as e:
        print(f"⚠️  Could not load biographical facts: {e}")

    session_id = "decomposition_dryrun"
    try:
        session_id = await container.session_store.get_latest_session_id(user_id) or session_id
    except Exception:
        pass

    def _make_message(text: str, sid: str) -> AgentMessage:
        return AgentMessage.create(
            sender="decomposition_dryrun_script",
            recipient=f"consolidation_agent_{user_id}",
            intent=AgentIntent.DELEGATE,
            payload={
                "messages": [{"role": "user", "text": text, "timestamp": time.time()}],
                "biographical_context": bio_facts,
            },
            context={
                "user_id": user_id,
                "account_id": account_id,
                "session_id": sid,
                "routing": {
                    "user_tone": "system",
                    "semantic_lens": ["biographical", "maintenance"],
                    "confidence": 1.0,
                },
            },
        )

    _original_call_llm = consolidation_agent._call_llm

    def _make_capturing(turns: List[Dict]):
        async def _capturing(request, turn=None):
            llm_response = await _original_call_llm(request, turn=turn)
            turns.append({
                "turn": turn,
                "text": llm_response.text or "",
                "tool_calls": [
                    {"name": tc.name, "args": tc.args}
                    for tc in (llm_response.tool_calls or [])
                ],
            })
            return llm_response
        return _capturing

    # ── --message mode: two-stage pipeline ───────────────────────────
    if raw_message:
        print(f"\n--- Input message ---")
        print(raw_message)
        print("---")

        # Stage 1: process raw message → extract operations
        print(f"\n{'='*65}")
        print("Stage 1: RAW MESSAGE → ConsolidationAgent (real reads, intercepted writes)")
        print(f"{'='*65}\n")

        llm_turns_1: List[Dict[str, Any]] = []
        dry_run_1 = DryRunFactManagementAdapter(real_fm)
        consolidation_agent._fact_management = dry_run_1
        consolidation_agent._call_llm = _make_capturing(llm_turns_1)

        t1 = time.time()
        async with RequestContext(user_id=user_id, account_id=account_id):
            await consolidation_agent.execute(_make_message(raw_message, f"{session_id}_stage1"))
        elapsed_1 = time.time() - t1
        print_summary(dry_run_1.operations, elapsed_1, 0)

        # Stage 2: pre-fetch cluster from stage 1 operations
        async with RequestContext(user_id=user_id, account_id=account_id):
            cluster = await prefetch_cluster_from_operations(real_fm, dry_run_1.operations, cluster_limit=30)

        if not cluster:
            print("No cluster found — nothing to review.")
            out_file = save_results([], dry_run_1.operations, llm_turns_1, elapsed_1, min_words)
            print(f"\nFull results → {out_file}")
            return

        cluster_msg = build_cluster_message(cluster)
        print(f"\n--- Cluster message preview (first 400 chars) ---")
        print(cluster_msg[:400] + "...")
        print("---")

        print(f"\n{'='*65}")
        print(f"Stage 2: CLUSTER REVIEW → ConsolidationAgent ({len(cluster)} facts, RFC prompt)")
        print(f"{'='*65}\n")

        llm_turns_2: List[Dict[str, Any]] = []
        dry_run_2 = ClusterDryRunAdapter(real_fm, cluster)
        consolidation_agent._fact_management = dry_run_2
        consolidation_agent._call_llm = _make_capturing(llm_turns_2)

        t2 = time.time()
        async with RequestContext(user_id=user_id, account_id=account_id):
            await consolidation_agent.execute(_make_message(cluster_msg, f"{session_id}_stage2"))
        elapsed_2 = time.time() - t2

        print_summary(dry_run_2.operations, elapsed_2, len(cluster))
        out_file = save_results(
            cluster,
            dry_run_2.operations,
            llm_turns_2,
            elapsed_1 + elapsed_2,
            min_words,
        )
        print(f"\nFull results → {out_file}")
        return

    # ── Normal mode: decomposition review ────────────────────────────
    facts = await fetch_long_facts(container.repository, account_id, min_words, limit)
    if not facts:
        print(f"No facts found with >{min_words} words. Nothing to process.")
        return

    user_message = build_user_message(facts)
    print(f"\n--- Message preview (first 500 chars) ---")
    print(user_message[:500] + ("..." if len(user_message) > 500 else ""))
    print("---\n")

    llm_turns: List[Dict[str, Any]] = []
    dry_run = DryRunFactManagementAdapter(real_fm)
    consolidation_agent._fact_management = dry_run
    consolidation_agent._call_llm = _make_capturing(llm_turns)
    print("DryRunFactManagementAdapter injected (real reads, intercepted writes).")

    print(f"\n{'='*65}")
    print(f"Running ConsolidationAgent [DRY-RUN] on {len(facts)} facts")
    print(f"{'='*65}\n")

    t0 = time.time()
    try:
        async with RequestContext(user_id=user_id, account_id=account_id):
            await consolidation_agent.execute(_make_message(user_message, session_id))
    except Exception as e:
        print(f"\n❌ Agent error: {e}")
        raise
    elapsed = time.time() - t0

    print_summary(dry_run.operations, elapsed, len(facts))
    out_file = save_results(facts, dry_run.operations, llm_turns, elapsed, min_words)
    print(f"\nFull results → {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ConsolidationAgent dry-run: review long facts for decomposition"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max number of facts to process (default: 30)",
    )
    parser.add_argument(
        "--min-words",
        type=int,
        default=40,
        help="Minimum word count threshold (default: 40)",
    )
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
    parser.add_argument(
        "--message",
        default=None,
        help="Raw user message to send instead of fetching long facts from Firestore",
    )
    args = parser.parse_args()

    if not args.user_id:
        print("ERROR: user_id required. Set DEV_USER_ID in .env or pass --user-id")
        sys.exit(1)
    if not args.account_id:
        print("ERROR: account_id required. Set DEV_ACCOUNT_ID in .env or pass --account-id")
        sys.exit(1)

    asyncio.run(
        main(
            limit=args.limit,
            min_words=args.min_words,
            user_id=args.user_id,
            account_id=args.account_id,
            raw_message=args.message,
        )
    )
