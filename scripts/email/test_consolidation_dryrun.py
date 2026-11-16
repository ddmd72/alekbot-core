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
    python scripts/email/test_consolidation_dryrun.py --s3-kit --limit 30
    python scripts/email/test_consolidation_dryrun.py --s3-kit --compare-forms --limit 20 --budget-usd 5

`--s3-kit` (Facts S3, plan T12b) does not use the POC facts file: it runs a real `consolidate_email` pass (pass label and
actor `email`) over the user's unconsolidated indexed emails, read from the live repository through a no-write wrapper (no
email is marked consolidated), on the dry-run kit; a report lands in `scripts/memory/`. With no unconsolidated email it says
so and stops. `--limit` is the batch size; `--facts-file` and `--category` do not apply. Without the flag the bench behaves
as it always did.

`--compare-forms` (with `--s3-kit`; email index v2 §6.7, slice 9, owner's Mac only) runs Stage 3 twice over one set of mail
and compares what memory would do with each form. The set S is a live read of the card batch (`memory_batch`), keeping the
carded mails whose card reads back (`--limit` caps the read); an empty S prints "nothing to compare" and spends nothing.
The line run sees S as the v1 one-line candidates (no card port, the email reads fixed to S); the card run sees S as cards
(a fixed card port over S, the live people list read only) under the `consolidation_email` prompt, whose token, profile and
blueprint are served from `prompts_snapshot/`. Each run is its own kit session (a fresh fact overlay, no email marked
live); the spend ledger is cumulative, so `--budget-usd` caps both together. The report (ops, the created and updated fact
texts with their reported_date, tokens and $ per run) goes to `scripts/memory/`; stdout prints counts and $ only. The kit's
residual applies: each model call writes its `prompt_content` row.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from google.cloud import firestore

from scripts.consolidation.legacy_dry_run_ports import install_dry_run
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.composition.service_container import ServiceContainer
from src.config.settings import load_settings
from src.domain.agent import AgentIntent, AgentMessage
from src.domain.request_context import RequestContext
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.domain.fact_write_context import FactWriteContext
from src.ports.fact_management_port import FactManagementPort
from src.composition.user_agent_factory import UserAgentFactory

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_bench_support import (
    BenchHandles,
    add_s3_arguments,
    biographical_facts,
    build_agent,
    execute,
    kit_message,
    kit_session,
    load_env,
    print_rows,
)
from scripts.consolidation.s3_dry_run_kit import (
    DryRunKit,
    NoWriteIndexedEmails,
    SnapshotBlueprintOverlay,
    SnapshotProfileOverlay,
    SnapshotTokenOverlay,
)
from src.domain.consolidation import EMAIL_PROFILE_ID
from src.domain.email import IndexedEmail
from src.domain.email_memory_candidate import MEMORY_CARD_WAIT, SETTLED_FOR_MEMORY, memory_card
from src.domain.email_memory_mail import MemoryMail
from src.ports.email_memory_batches import EmailMemoryBatches

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
        *,
        timezone: str,
    ) -> List[Dict[str, Any]]:
        results = await self._real.search_existing_facts(
            keywords, primary_query, alternative_query, limit, timezone=timezone
        )
        print(f"    🔍 search({primary_query!r}) → {len(results)} existing facts")
        return results

    async def create_fact(
        self, content: str, metadata: Dict[str, Any], *, context: FactWriteContext
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
        self, fact_id: str, updates: Dict[str, Any], account_id: str, *, context: FactWriteContext
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
        *,
        context: FactWriteContext,
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
# S3 kit mode: a real `consolidate_email` pass over the unconsolidated indexed emails
# ─────────────────────────────────────────────────────────────────────────────

def build_email_message(kit, bio_facts: List[Dict], user_id: str, account_id: str, limit: int):
    """The S3 bench's message: a `consolidate_email` pass of one batch (pass label and actor `email`)."""
    return kit_message(
        kit, sender="email_dryrun_script", user_id=user_id, account_id=account_id, task="consolidate_email",
        payload={"number_of_batches": 1, "batch_size": limit, "biographical_context": bio_facts},
        session_id="email_dryrun",
    )


async def has_unconsolidated_emails(email_repo, user_id: str) -> bool:
    return bool(await email_repo.get_unconsolidated_batch(user_id, limit=1))


async def run_s3_kit(handles, limit: int, args) -> None:
    print(f"\n{'='*65}\nEMAIL CONSOLIDATION ON THE S3 KIT (consolidate_email pass, no email marked)\n{'='*65}")
    user_id, account_id = args.user_id, args.account_id
    if handles.agent._indexed_email_repo is None:
        print("ERROR: the agent has no indexed-email repository: nothing to run.")
        return
    if not await has_unconsolidated_emails(handles.agent._indexed_email_repo, user_id):
        print("No unconsolidated emails: bench 5 is skipped (nothing was run, nothing was spent).")
        return
    bio_facts = await biographical_facts(handles, account_id)

    async with kit_session(handles, args, user_id, account_id, "email") as (kit, extra):
        extra.update({"model": handles.agent.model_name, "batch_size": limit})
        first_row = len(kit.rows)
        response = await execute(
            handles.agent, build_email_message(kit, bio_facts, user_id, account_id, limit),
            user_id, account_id, "consolidate_email",
        )
        extra["result"] = response.result
        print(f"\n  result: {response.result}")
        print_rows(kit.rows[first_row:])
        print(f"  emails the kit would have marked consolidated: "
              f"{sum(len(w['email_ids']) for w in (kit.emails.writes if kit.emails else []))} (none marked live)")


async def main_s3_kit(limit: int, args) -> None:
    await run_s3_kit(await build_agent(args.user_id), limit, args)


# ─────────────────────────────────────────────────────────────────────────────
# Compare forms: the same mail as one-line candidates and as cards (slice 9)
# ─────────────────────────────────────────────────────────────────────────────

CARD_TOKEN_ID = "CONSOLIDATION_EMAIL_CANDIDATES"
CARD_BLUEPRINT_ID = "consolidation_email_v1"


class FixedEmails(NoWriteIndexedEmails):
    """The email reads of a compare run: the unconsolidated batch is the compare set; every other read is live and every
    write is recorded only."""

    def __init__(self, real, mails: List[MemoryMail]) -> None:
        super().__init__(real)
        self._mails = list(mails)

    async def get_unconsolidated_batch(self, user_id: str, limit: int = 200) -> List[IndexedEmail]:
        return [m.email for m in self._mails if m.email.user_id == user_id][:limit]


class FixedMemoryBatches(EmailMemoryBatches):
    """The card run's card reads: the compare set, whatever the live store holds now."""

    def __init__(self, mails: List[MemoryMail]) -> None:
        self._mails = list(mails)

    async def memory_batch(self, user_id, limit, settled, stale_before) -> List[MemoryMail]:
        return [m for m in self._mails if m.email.user_id == user_id][:limit]


async def select_compare_set(batches: EmailMemoryBatches, user_id: str, limit: int,
                             now: Optional[datetime] = None) -> List[MemoryMail]:
    """The live card batch, kept to the carded mails whose card reads back."""
    moment = now or datetime.now(timezone.utc)
    mails = await batches.memory_batch(user_id, limit, SETTLED_FOR_MEMORY, moment - MEMORY_CARD_WAIT)
    return [m for m in mails if memory_card(m) is not None]


def fix_form(agent: Any, form: str, mails: List[MemoryMail]) -> None:
    """Point the agent's email reads at the compare set: `line` with no card port, `card` with a fixed one."""
    agent._indexed_email_repo = FixedEmails(agent._indexed_email_repo, mails)
    agent._email_memory_batches = FixedMemoryBatches(mails) if form == "card" else None


def serve_card_prompt(service: Any, snapshot_dir: Path = kit_module.SNAPSHOT_DIR) -> None:
    """Serve the card token, profile and blueprint from the snapshot (on top of the kit's own overlays)."""
    service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, snapshot_dir,
                                              token_ids=(CARD_TOKEN_ID,))
    service.profile_repo = SnapshotProfileOverlay(service.profile_repo, snapshot_dir, agent_ids=(EMAIL_PROFILE_ID,))
    service.blueprint_repo = SnapshotBlueprintOverlay(service.blueprint_repo, snapshot_dir,
                                                      blueprint_ids=(CARD_BLUEPRINT_ID,))
    service.invalidate_cache()


class TokenTally:
    """Adds up the token counts of the agent's model calls (installed after the kit, so it sees the metered calls)."""

    def __init__(self, agent: Any) -> None:
        self.prompt = self.completion = self.calls = 0
        inner = agent._call_llm

        async def tallied(request, *args, **kwargs):
            response = await inner(request, *args, **kwargs)
            usage = getattr(response, "usage_metadata", None)
            self.calls += 1
            if usage is not None:
                self.prompt += usage.prompt_tokens or 0
                self.completion += usage.completion_tokens or 0
            return response

        agent._call_llm = tallied


async def form_outcome(kit: DryRunKit, tally: TokenTally, response: Any) -> Dict[str, Any]:
    """What one run did: its ops, the facts it created or updated (text and reported_date), tokens and $."""
    facts = []
    for row in kit.rows:
        fact = await kit.repo.get_fact_by_id(row["fact_id"]) if row.get("fact_id") and kit.repo else None
        facts.append({"action": row["action"], "status": row["status"],
                      "text": fact.text if fact is not None else None,
                      "reported_date": row.get("reported_date_chosen") or row.get("reported_date_sent")})
    return {
        "result": response.result,
        "ops": dict(Counter(f"{r['action']}:{r['status']}" for r in kit.rows)),
        "facts": facts,
        "tokens": {"prompt": tally.prompt, "completion": tally.completion, "calls": tally.calls},
        "usd": kit.meter.run_usd,
        "email_ids_read": sorted({e for w in (kit.emails.writes if kit.emails else [])
                                  if w["kind"] == "mark_consolidated" for e in w["email_ids"]}),
    }


async def run_form(handles: BenchHandles, args, form: str, mails: List[MemoryMail]) -> Dict[str, Any]:
    user_id, account_id = args.user_id, args.account_id
    fix_form(handles.agent, form, mails)
    bio_facts = await biographical_facts(handles, account_id)
    async with kit_session(handles, args, user_id, account_id, f"compare_{form}") as (kit, extra):
        if form == "card":
            serve_card_prompt(handles.agent.prompt_builder.assembly_service)
        tally = TokenTally(handles.agent)
        response = await execute(
            handles.agent, build_email_message(kit, bio_facts, user_id, account_id, len(mails)),
            user_id, account_id, f"consolidate_email ({form} form)",
        )
        outcome = await form_outcome(kit, tally, response)
        extra.update(outcome)
    return outcome


async def compare_forms(build: Callable[[], Awaitable[BenchHandles]], args) -> Optional[Path]:
    """Select the compare set, run the line form then the card form over it, write the report. None when S is empty."""
    handles = await build()
    live = handles.agent._indexed_email_repo
    if not isinstance(live, EmailMemoryBatches):
        print("ERROR: the agent's email repository has no card reads: nothing to compare.")
        return None
    mails = await select_compare_set(live, args.user_id, args.limit)
    if not mails:
        print("nothing to compare: no unconsolidated carded mail with a readable card (nothing run, nothing spent)")
        return None
    print(f"compare set: {len(mails)} carded mails")
    outcomes = {"line": await run_form(handles, args, "line", mails)}
    outcomes["card"] = await run_form(await build(), args, "card", mails)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = kit_module.inside_memory_dir(kit_module.MEMORY_DIR / f"consolidation_compare_forms_{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"email_ids": [m.email.email_id for m in mails], "forms": outcomes},
                               indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    for form, outcome in outcomes.items():
        print(f"  {form}: ops {outcome['ops']}  tokens {outcome['tokens']['prompt']}+{outcome['tokens']['completion']}"
              f"  ${outcome['usd']:.4f}")
    print(f"  compare report -> {path}")
    return path


async def main_compare_forms(args) -> None:
    await compare_forms(lambda: build_agent(args.user_id), args)


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
    install_dry_run(consolidation_agent, dry_run)
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
    load_env()
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
    add_s3_arguments(parser)
    parser.add_argument("--compare-forms", action="store_true",
                        help="with --s3-kit: run Stage 3 over the same carded mail as one-line candidates and as cards")
    args = parser.parse_args()

    if not args.user_id:
        print("ERROR: user_id required. Set DEV_USER_ID in .env or pass --user-id")
        sys.exit(1)
    if not args.account_id:
        print("ERROR: account_id required. Set DEV_ACCOUNT_ID in .env or pass --account-id")
        sys.exit(1)

    if args.compare_forms:
        if not args.s3_kit:
            parser.error("--compare-forms runs on the kit: add --s3-kit")
        asyncio.run(main_compare_forms(args))
    elif args.s3_kit:
        if args.category:
            parser.error("--category does not apply with --s3-kit (the pass reads the indexed emails, not the facts file)")
        asyncio.run(main_s3_kit(args.limit, args))
    else:
        asyncio.run(
            main(
                facts_file=Path(args.facts_file),
                limit=args.limit,
                category=args.category,
                user_id=args.user_id,
                account_id=args.account_id,
            )
        )
