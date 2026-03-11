#!/usr/bin/env python3
"""
ConsolidationAgent Dry-Run on Email Facts
==========================================
Loads classified email facts from a POC JSON file, feeds them to
ConsolidationAgent as a system_alert message. Fact *reads* (search_existing_facts)
hit the real Firestore. Fact *writes* (create/update/merge/discard) are intercepted
and logged — nothing is written.

Usage:
    python scripts/email/test_consolidation_dryrun.py
    python scripts/email/test_consolidation_dryrun.py --limit 30
    python scripts/email/test_consolidation_dryrun.py --limit 50 --category healthcare
    python scripts/email/test_consolidation_dryrun.py --facts-file scripts/memory/email_facts_OTHER.json
    python scripts/email/test_consolidation_dryrun.py --user-id <user_id>

Pre-conditions:
    .env must have: DEV_USER_ID, DEV_ACCOUNT_ID, FIRESTORE_DATABASE, GEMINI_API_KEY
    Facts file must be the output of test_email_classification_poc.py (--save flag)
"""

import argparse
import asyncio
import json
import os
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

# Default facts file — most recent POC output
DEFAULT_FACTS_FILE = Path("scripts/memory/email_facts_20260228_002314.json")

SYSTEM_ALERT_HEADER = (
    "[system_alert] Система по поручению пользователя автоматически просканировала "
    "его ящик электронной почты и сделала выборку кандидатов для занесения в базу фактов. "
    "Выборка неоднородна и содержит много шума. Ты единственный кто может принять решение "
    "о ценности этой информации. Оцени входящие данные и обработай по своему алгоритму.\n\n"
    "Кандидаты:"
)


# ─────────────────────────────────────────────────────────────────────────────
# Dry-Run Adapter
# ─────────────────────────────────────────────────────────────────────────────

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
        # Omit vector fields from preview
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

def load_facts(
    facts_file: Path,
    limit: int,
    category: Optional[str],
) -> tuple[List[Dict], Dict]:
    with facts_file.open(encoding="utf-8") as f:
        data = json.load(f)
    facts = data.get("facts", [])
    meta = {
        "source_file": facts_file.name,
        "generated_at": data.get("generated_at", "?"),
        "query": data.get("query", "?"),
        "total_valuable": len(facts),
    }
    if category:
        facts = [f for f in facts if f.get("category") == category]
    return facts[:limit], meta


def build_user_message(facts: List[Dict]) -> str:
    lines = [SYSTEM_ALERT_HEADER, ""]
    for i, fact in enumerate(facts, 1):
        obj = {
            "fact": fact["fact"],
            "category": fact.get("category"),
            "tags": fact.get("tags", []),
            "date": fact.get("metadata", {}).get("date", ""),
        }
        lines.append(f"{i}. {json.dumps(obj, ensure_ascii=False)}")
    return "\n".join(lines)


def print_summary(operations: List[Dict], elapsed: float) -> None:
    counts = Counter(op["action"] for op in operations)
    print("\n" + "=" * 65)
    print("DRY-RUN SUMMARY")
    print("=" * 65)
    print(f"  Elapsed: {elapsed:.1f}s")
    print(f"  Operations: {len(operations)}")
    for action in ["CREATE", "UPDATE", "MERGE", "DISCARD"]:
        n = counts.get(action, 0)
        if n:
            print(f"    {action}: {n}")

    if not operations:
        print("  (no operations — agent may have decided nothing is worth storing)")
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


def save_results(
    facts: List[Dict],
    operations: List[Dict],
    elapsed: float,
    meta: Dict,
) -> Path:
    out_file = (
        Path("scripts/memory")
        / f"consolidation_dryrun_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    out_file.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "source": meta,
                "facts_input": len(facts),
                "elapsed_s": round(elapsed, 1),
                "operations_count": len(operations),
                "operations": operations,
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
    facts_file: Path,
    limit: int,
    category: Optional[str],
    user_id: str,
    account_id: str,
) -> None:
    # ── Load facts ────────────────────────────────────────────────────
    facts, meta = load_facts(facts_file, limit, category)
    if not facts:
        print(f"No facts found (file={facts_file}, category={category}).")
        return

    print(f"\n{'='*65}")
    print("EMAIL → CONSOLIDATION DRY-RUN")
    print(f"{'='*65}")
    print(f"  Source:    {meta['source_file']}  (generated {meta['generated_at'][:10]})")
    print(f"  Total valuable in file: {meta['total_valuable']}")
    print(f"  Category filter: {category or 'all'}")
    print(f"  Facts to process: {len(facts)}")
    print(f"  User: {user_id}")

    user_message = build_user_message(facts)
    print(f"\n--- Message preview (first 400 chars) ---")
    print(user_message[:400] + ("..." if len(user_message) > 400 else ""))
    print("---\n")

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

    # ── Factory ───────────────────────────────────────────────────────
    factory = UserAgentFactory(
        config=config,
        env_config=env_config,
        coordinator=coordinator,
        user_repo=user_repo,
        account_repo=account_repo,
        **container.agent_services(),
    )

    # ── Create agents ─────────────────────────────────────────────────
    print("Creating agents...")
    agents = await factory.ensure_agents_for_user(user_id)
    consolidation_agent = agents.get("consolidation_agent")
    if not consolidation_agent:
        print(f"consolidation_agent not found. Keys: {list(agents.keys())}")
        return

    # ── Inject DryRun adapter ─────────────────────────────────────────
    real_fm = consolidation_agent._fact_management
    if real_fm is None:
        print("ERROR: _fact_management is None — agent was built without FactManagementPort.")
        print("Check that fact_management_adapter_factory is wired in ServiceContainer.")
        return

    dry_run = DryRunFactManagementAdapter(real_fm)
    consolidation_agent._fact_management = dry_run
    print(f"DryRunFactManagementAdapter injected (real reads, intercepted writes).")

    # ── Load biographical context (real Firestore) ────────────────────
    bio_facts = []
    try:
        bio_facts = await container.repository.get_biographical_context_cached(
            account_id, limit=100
        )
        print(f"Loaded {len(bio_facts)} biographical facts for context.")
    except Exception as e:
        print(f"⚠️  Could not load biographical facts: {e}")

    # ── Build message ────────────────────────────────────────────────
    session_id = "dryrun_session"
    try:
        session_id = await container.session_store.get_latest_session_id(user_id) or session_id
    except Exception:
        pass

    message = AgentMessage.create(
        sender="email_dryrun_script",
        recipient=f"consolidation_agent_{user_id}",
        intent=AgentIntent.DELEGATE,
        payload={
            "messages": [
                {
                    "role": "user",
                    "text": user_message,
                    "timestamp": time.time(),
                }
            ],
            "biographical_context": bio_facts,
        },
        context={
            "user_id": user_id,
            "account_id": account_id,
            "session_id": session_id,
            "routing": {
                "user_tone": "system",
                "semantic_lens": ["email", "biographical"],
                "confidence": 1.0,
            },
        },
    )

    # ── Run ───────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    print(f"Running ConsolidationAgent [DRY-RUN] on {len(facts)} email facts")
    print(f"{'='*65}\n")

    t0 = time.time()
    try:
        async with RequestContext(user_id=user_id, account_id=account_id):
            response = await consolidation_agent.execute(message)
    except Exception as e:
        print(f"\n❌ Agent error: {e}")
        raise
    elapsed = time.time() - t0

    if response:
        print(f"\nAgent response (raw): {str(response)[:500]}")

    print_summary(dry_run.operations, elapsed)

    # ── Save ──────────────────────────────────────────────────────────
    out_file = save_results(facts, dry_run.operations, elapsed, meta)
    print(f"\nFull results → {out_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ConsolidationAgent dry-run on classified email facts"
    )
    parser.add_argument(
        "--facts-file",
        default=str(DEFAULT_FACTS_FILE),
        help="Path to email facts JSON (output of test_email_classification_poc.py --save)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Max number of email facts to process (default: 30)",
    )
    parser.add_argument(
        "--category",
        help="Filter by category: travel|finance|healthcare|work|legal|personal|subscription",
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
    args = parser.parse_args()

    if not args.user_id:
        print("ERROR: user_id required. Set DEV_USER_ID in .env or pass --user-id")
        sys.exit(1)
    if not args.account_id:
        print("ERROR: account_id required. Set DEV_ACCOUNT_ID in .env or pass --account-id")
        sys.exit(1)

    asyncio.run(
        main(
            facts_file=Path(args.facts_file),
            limit=args.limit,
            category=args.category,
            user_id=args.user_id,
            account_id=args.account_id,
        )
    )
