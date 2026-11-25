#!/usr/bin/env python3
"""
Memory inbox final-pass dry run (MCP_MEMORY_SUBMISSIONS_RFC §9; the delivery plan's U4 step 5). PAID: every run calls the
consolidation model. Run it only on the owner's OK, after the eval table and the probe estimate.

What runs: the real `ConsolidationAgent` `consolidate_memory_inbox` task (its prompt build, loop, the normal fact tools,
actor `inbox` and the `reported_date` span) over synthetic confirmed candidates, as production builds the agent
(`s3_bench_support.build_agent`). What is held back:

- No inbox store is read or written: the candidates come from the input file; nothing is marked and nothing reaches the
  owner.
- Fact writes are intercepted by the S3 dry-run kit (`DryRunKit`): reads see the configured fact store, every create, update and
  merge goes to its in-memory overlay (`NoWriteFactRepository`) and is one report row (`WriteRecorder`: what was sent,
  what was stored, the `reported_date` sent and chosen). The baseline refresh, the counters and the alert sink are the
  kit's recording stand-ins.
- The prompt: the five S3 tokens come from `prompts_snapshot/` (the kit), and so does `CONSOLIDATION_INBOX_CANDIDATES`
  (`SnapshotTokenOverlay`), so the token is tuned without an upload; the `consolidation_inbox` profile and blueprint
  are served from the snapshot too, unless `--profile-from-firestore` reads the uploaded ones.
- Cost: the kit's ledger keeps the cumulative cap across kit runs (`--budget-usd`); on top, every call of this run is
  priced with its token legs (input, output, cache read, cache write) and the run stops before a call that could take
  its spend past `--max-usd`, or when the model has no price. The legs and totals are printed and saved.

The risk under test is the `Domain_Scope` fallback "when in doubt, DISCARD": the report counts what was captured (a
create, update or merge that wrote), what the model's final report discarded and why, and, for each dated pair of the
input, whether the earlier statement became history (archived, superseded or kept as history in an update) or was
invalidated (never true). Residual: the agent's `_call_llm` still writes one `prompt_content` row per call (as every kit
bench); `search_existing_facts` reads the configured fact store, which holds no synthetic fact, so a `relation` naming a fact id
cannot be followed here.

Input: a JSON file in `scripts/memory/` (gitignored), examples from the invented universe only:
    {"pairs": {"sunday_play": ["tennis", "Sunday"]},              # a dated pair: words its written facts carry
     "candidates": [{"id": "k1", "received_days_ago": 9, "text": "...", "said_when": "", "happens_when": "",
                     "basis": "said", "edited": false, "pair": "sunday_play",
                     "draft": {"directive": false, "temporal_class": "dynamic", "context_priority": "medium",
                               "expiry": {"kind": "horizon", "days": 90}},
                     "relation": {"kind": "new"}}, ...]}

Usage:
    python scripts/consolidation/test_inbox_final_dryrun.py --candidates scripts/memory/inbox_final_candidates.json \
        --max-usd 2 [--probe] [--timezone Europe/Lisbon] [--profile-from-firestore] [--budget-usd 30]

`--probe` stops the pass after its first model call: one measured call (the fixed prompt plus the message) for the
estimate. Report: `scripts/memory/inbox_final_dry_run_report.json`. Pre-conditions: `.env` with DEV_USER_ID,
DEV_ACCOUNT_ID, FIRESTORE_DATABASE and the consolidation model's key.
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.consolidation.s3_bench_support import build_agent, load_env  # noqa: E402
from scripts.consolidation.s3_dry_run_kit import (  # noqa: E402
    MEMORY_DIR,
    SNAPSHOT_DIR,
    DryRunKit,
    SnapshotBlueprintOverlay,
    SnapshotProfileOverlay,
    SnapshotTokenOverlay,
    add_kit_arguments,
    check_budget_flag,
    inside_memory_dir,
)
from scripts.consolidation.test_inbox_triage_dryrun import RunMeter  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage, AgentStatus  # noqa: E402
from src.domain.consolidation import FINAL_AGENT_TASK, INBOX_PROFILE_ID, consolidation_agent_id  # noqa: E402
from src.domain.fact_tool_result import STATUS_CREATED, STATUS_MERGED, STATUS_UPDATED  # noqa: E402
from src.domain.memory_inbox import (  # noqa: E402
    ANSWER_WINDOW,
    FINAL_BATCH,
    Basis,
    Candidate,
    CandidateFields,
    CandidateStatus,
    DraftClass,
    Relation,
    RelationKind,
    SourceCopy,
)
from src.domain.memory_inbox_final_task import decode_final_result, encode_final_task, final_span  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402

INBOX_TOKEN_IDS: Tuple[str, ...] = ("CONSOLIDATION_INBOX_CANDIDATES",)
INBOX_BLUEPRINT_ID = "consolidation_inbox_v1"
DEFAULT_CANDIDATES = MEMORY_DIR / "inbox_final_candidates.json"
DEFAULT_REPORT = MEMORY_DIR / "inbox_final_dry_run_report.json"
WRITTEN = {STATUS_CREATED, STATUS_UPDATED, STATUS_MERGED}
PROMPT_MARKERS = ("fact_taxonomy", "rule Domain_Scope", "Inbox_Candidates")


# ------------------------------------------------------------------------------------------------ input


def _basis(value: Any) -> Basis:
    return Basis(value) if value in {b.value for b in Basis} else Basis.INFERRED


def load_candidates(path: Path, now: datetime) -> Tuple[Tuple[Candidate, ...], Dict[str, List[str]], Dict[str, str]]:
    """The synthetic confirmed candidates (received `received_days_ago` before `now`, confirmed half a day later, never
    after `now`), the dated pairs' words, and each candidate's pair."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("candidates", [])
    if not items or len(items) > FINAL_BATCH:
        raise ValueError(f"the input needs 1 to {FINAL_BATCH} candidates (one pass), got {len(items)}")
    candidates = tuple(_candidate(item, now) for item in items)
    pair_of = {item["id"]: item["pair"] for item in items if item.get("pair")}
    pairs = {name: list(words) for name, words in raw.get("pairs", {}).items()}
    unknown = sorted(set(pair_of.values()) - set(pairs))
    if unknown:
        raise ValueError(f"candidates name pairs with no words in 'pairs': {', '.join(unknown)}")
    return candidates, pairs, pair_of


def _candidate(item: Dict[str, Any], now: datetime) -> Candidate:
    received = now - timedelta(days=float(item.get("received_days_ago", 0)))
    created = received + timedelta(hours=1)
    basis = _basis(item.get("basis"))
    text, said, happens = item["text"], item.get("said_when", ""), item.get("happens_when", "")
    source = SourceCopy(submission_id=f"src-{item['id']}", received_at=received, client_name="Claude",
                        information=text, basis=basis, said_when=said, happens_when=happens)
    relation = item.get("relation") or {"kind": "new"}
    edited = bool(item.get("edited", False))
    return Candidate(
        candidate_id=item["id"], user_id="dry-run", account_id="dry-run", status=CandidateStatus.CONFIRMED,
        created_at=created, answer_by=created + ANSWER_WINDOW, language="en", source_ids=(source.submission_id,),
        sources=(source,), text=text, happens_when=happens, said_when=said, basis=basis,
        draft=DraftClass(**item["draft"]),
        relation=Relation(RelationKind(relation["kind"]), relation.get("fact_id"), relation.get("fact_text")),
        proposed=CandidateFields(text=f"(triage's wording of {item['id']})", happens_when=happens, said_when=said,
                                 basis=basis) if edited else None,
        edited=edited, confirmed_at=min(received + timedelta(hours=12), now),
    )


# ------------------------------------------------------------------------------------------------ the agent


def install(agent: Any, kit: DryRunKit, from_firestore: bool) -> None:
    """The kit under `agent`, then the inbox token (and, unless `from_firestore`, the inbox profile and blueprint) from
    the snapshot on top of the kit's overlay."""
    kit.install(agent)
    service = agent.prompt_builder.assembly_service
    service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, SNAPSHOT_DIR, INBOX_TOKEN_IDS)
    if not from_firestore:
        service.profile_repo = SnapshotProfileOverlay(service.profile_repo, SNAPSHOT_DIR, (INBOX_PROFILE_ID,))
        service.blueprint_repo = SnapshotBlueprintOverlay(service.blueprint_repo, SNAPSHOT_DIR, (INBOX_BLUEPRINT_ID,))
    service.invalidate_cache()


def keep_operations(agent: Any, into: List[Dict[str, Any]]) -> None:
    """Keep the model's final report (the loop's operations) of the pass: its DISCARD entries say what it dropped."""
    loop = agent._run_consolidation_loop

    async def run_loop(*args: Any, **kwargs: Any) -> Any:
        operations = await loop(*args, **kwargs)
        into.extend(op for op in (operations or []) if isinstance(op, dict))
        return operations

    agent._run_consolidation_loop = run_loop


async def check_prompt(agent: Any, user_id: str, account_id: str) -> int:
    """The inbox prompt builds and holds the taxonomy, Domain_Scope and the inbox rule (no model call); its length."""
    prompt = await agent.prompt_builder.build_for_agent(
        agent_type=INBOX_PROFILE_ID, user_id=user_id, account_id=account_id, routing_metadata=None,
        include_directives=False, biographical_facts=[], conversation_history=[])
    missing = [marker for marker in PROMPT_MARKERS if marker not in (prompt or "")]
    if missing:
        raise RuntimeError(f"the inbox prompt lacks {', '.join(missing)}: check the profile and the blueprint")
    return len(prompt)


# ------------------------------------------------------------------------------------------------ the report


def written_rows(kit: DryRunKit) -> List[Dict[str, Any]]:
    """The kit's write rows that wrote, each with the stored content of the fact it wrote."""
    rows = []
    for row in kit.rows:
        if row.get("status") not in WRITTEN:
            continue
        stored = kit.repo.overlay_document(row["fact_id"]) if kit.repo is not None and row.get("fact_id") else None
        rows.append({**row, "content": stored.text if stored is not None else None})
    return rows


def counts(rows: Sequence[Dict[str, Any]], operations: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Captured (writes by action and by the state they left), discarded (the final report's DISCARD reasons)."""
    discards = [str(op.get("reason", "")) for op in operations if str(op.get("action", "")).upper() == "DISCARD"]
    return {
        "captured": dict(Counter(row["action"] for row in rows)),
        "states_written": dict(Counter(str(row.get("state_stored")) for row in rows)),
        "invalidated": sum(1 for row in rows if row.get("state_stored") == "invalidated"),
        "to_history": sum(1 for row in rows if row.get("state_stored") in ("archived", "superseded")),
        "discarded": len(discards),
        "discard_reasons": discards,
        "creates_without_reported_date": sum(1 for row in rows if row["action"] == "create"
                                             and not row.get("reported_date_sent")),
    }


def pair_outcomes(rows: Sequence[Dict[str, Any]], pairs: Dict[str, List[str]]) -> Dict[str, Dict[str, Any]]:
    """Per dated pair: the writes whose stored content carries one of its words, and what the earlier statement became
    (`invalidated`, `history` when a write archived or superseded a fact or updated one in place, else `created_only`
    or `nothing`). A rough reading for the eval table: the rows are listed for the owner's own reading."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, words in pairs.items():
        hits = [row for row in rows if row.get("content") and any(w.lower() in row["content"].lower() for w in words)]
        states = {row.get("state_stored") for row in hits}
        if "invalidated" in states:
            verdict = "invalidated"
        elif states & {"archived", "superseded"} or any(row["action"] in ("update", "merge") for row in hits):
            verdict = "history"
        else:
            verdict = "created_only" if hits else "nothing"
        out[name] = {"verdict": verdict, "writes": [
            {"action": row["action"], "state": row.get("state_stored"), "content": row["content"],
             "reported_date_sent": row.get("reported_date_sent"),
             "reported_date_chosen": row.get("reported_date_chosen")} for row in hits]}
    return out


async def main(args: argparse.Namespace) -> None:
    candidates_path = inside_memory_dir(Path(args.candidates))
    report_path = inside_memory_dir(Path(args.report))
    kit = DryRunKit(budget_usd=check_budget_flag(args.budget_usd, args.allow_over_owner_cap))
    try:
        handles = await build_agent(args.user_id)
        agent = handles.agent
        zone = await kit.resolve_timezone(handles.account_repo, handles.user_repo, args.account_id, args.timezone)
        now = datetime.now(timezone.utc)
        candidates, pairs, pair_of = load_candidates(candidates_path, now)
        install(agent, kit, args.profile_from_firestore)
        operations: List[Dict[str, Any]] = []
        keep_operations(agent, operations)
        meter = RunMeter(args.max_usd)
        meter.wrap(agent)
        if args.probe:
            agent.MAX_CONSOLIDATION_TURNS = 1
        await kit.prepare(args.user_id, args.account_id)
        prompt_chars = await check_prompt(agent, args.user_id, args.account_id)
        first, last = final_span(candidates)
        print(f"  inbox prompt {prompt_chars} chars, zone {zone}, {len(candidates)} candidates, "
              f"{len(pair_of)} in {len(pairs)} dated pairs, span {first.date()}..{last.date()}")

        message = AgentMessage.create(
            sender="inbox_final_dry_run", recipient=consolidation_agent_id(args.user_id), intent=AgentIntent.DELEGATE,
            payload={"task": FINAL_AGENT_TASK, **encode_final_task(candidates)},
            context=kit.message_context(args.user_id, args.account_id),
        )
        report: Dict[str, Any] = {"started": now.isoformat(), "zone": zone, "probe": args.probe,
                                  "prompt_chars": prompt_chars, "candidates": len(candidates)}
        try:
            async with RequestContext(user_id=args.user_id, account_id=args.account_id):
                response = await agent.execute(message)
            report["status"] = response.status.value
            if response.status is AgentStatus.SUCCESS:
                ended, wrote = decode_final_result(response.result)
                report.update(ended=ended, wrote=wrote)
            else:
                report["error"] = response.error
        finally:
            rows = written_rows(kit)
            report.update(counts=counts(rows, operations), pairs=pair_outcomes(rows, pairs), operations=operations,
                          cost={"calls": meter.calls, "totals": meter.totals()}, kit=kit.report())
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    finally:
        kit.close()

    for row in rows:
        print(f"  {row['action']:<6} {str(row.get('state_stored')):<12} reported "
              f"{row.get('reported_date_sent') or '-'} -> {row.get('reported_date_chosen')} | {row['content']}")
    summary = report["counts"]
    print(f"\n  captured={summary['captured']} states={summary['states_written']} discarded={summary['discarded']} "
          f"creates_without_reported_date={summary['creates_without_reported_date']}")
    for name, outcome in report["pairs"].items():
        print(f"  pair {name}: {outcome['verdict']} ({len(outcome['writes'])} writes)")
    totals = meter.totals()
    print(f"\n  status={report.get('status')} ended={report.get('ended')} wrote={report.get('wrote')}")
    print(f"  cost ${totals['usd']:.4f} over {totals['calls']} calls: in={totals['prompt_tokens']} "
          f"out={totals['completion_tokens']} cache_read={totals['cache_read_tokens']} "
          f"cache_write={totals['cache_creation_tokens']} unmetered={totals['unmetered_calls']}")
    print(f"  report -> {report_path}")


if __name__ == "__main__":
    load_env()
    parser = argparse.ArgumentParser(description="Memory inbox final-pass dry run (PAID: calls the consolidation model)")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES),
                        help="the synthetic input JSON under scripts/memory/")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="the report JSON under scripts/memory/")
    parser.add_argument("--max-usd", type=float, required=True,
                        help="hard stop: no model call that could take this run's spend past it")
    parser.add_argument("--probe", action="store_true", help="stop after the first model call (the estimate's probe)")
    parser.add_argument("--profile-from-firestore", action="store_true",
                        help="read the uploaded inbox profile and blueprint instead of the snapshot's")
    parser.add_argument("--user-id", default=os.getenv("DEV_USER_ID"), help="default: DEV_USER_ID")
    parser.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"), help="default: DEV_ACCOUNT_ID")
    add_kit_arguments(parser)
    parsed = parser.parse_args()
    if not parsed.user_id or not parsed.account_id:
        parser.error("set DEV_USER_ID and DEV_ACCOUNT_ID in .env or pass --user-id and --account-id")
    if parsed.max_usd <= 0:
        parser.error("--max-usd must be above 0")
    asyncio.run(main(parsed))
