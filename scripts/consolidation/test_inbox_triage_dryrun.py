#!/usr/bin/env python3
"""
Memory inbox triage dry run (MCP_MEMORY_SUBMISSIONS_RFC §7; the delivery plan's U3 step 6). PAID: every run calls the
consolidation model. Run it only on the owner's OK, after the eval table and the probe estimate.

What runs: the real `ConsolidationAgent` `triage_memory_inbox` task (its prompt build, loop, tool routing and the triage
scope) over synthetic submissions, as production builds the agent (`s3_bench_support.build_agent`). What is held back:

- No inbox store is read or written: the submissions, waiting and answered candidates come from the input file, and the
  decisions are only printed and saved. Nothing reaches the owner.
- The fact store is the dry-run kit's `NoWriteFactRepository` (reads the configured fact store, writes to an overlay; the triage writes none
  anyway: every fact-writing tool is unknown to it), the counters and the alert sink are recording stand-ins.
- The prompt: `CONSOLIDATION_INBOX_TRIAGE` is served from `prompts_snapshot/` (`SnapshotTokenOverlay`), so the token is
  tuned without an upload; the profile and the blueprint are served from the snapshot too, unless
  `--profile-from-firestore` reads the uploaded ones. `CONSOLIDATION_TAXONOMY` comes from Firestore, as in production.
- Cost: every model call is priced (`domain.billing.calculate_cost`) with its token legs (input, output, cache read,
  cache write); before each call the run stops when its spend plus its largest call so far would pass `--max-usd`, or
  when the model has no price. The legs and totals are printed and saved.

Residual: the agent's `_call_llm` still writes one `prompt_content` row per call (as every kit bench). The `known`
discard cannot be measured here: `search_existing_facts` reads the configured fact store, which holds no synthetic fact; it is
checked after rollout.

Input: a JSON file in `scripts/memory/` (gitignored), examples from the invented universe only:
    {"language": "en",
     "submissions": [{"id": "s1", "received_hours_ago": 30, "client": "Claude", "information": "...",
                      "basis": "said", "said_when": "", "happens_when": ""}, ...],
     "waiting":  [{"id": "k1", "text": "..."}],
     "answered": [{"text": "...", "answered": "rejected"}]}

Usage:
    python scripts/consolidation/test_inbox_triage_dryrun.py --submissions scripts/memory/inbox_triage_submissions.json \
        --max-usd 2 [--language uk] [--probe] [--timezone Europe/Lisbon] [--profile-from-firestore]

`--probe` stops the pass after its first model call: one measured call (the fixed prompt plus the message) for the
estimate. Report: `scripts/memory/inbox_triage_dry_run_report.json`. Pre-conditions: `.env` with DEV_USER_ID,
DEV_ACCOUNT_ID, FIRESTORE_DATABASE and the consolidation model's key.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.consolidation.s3_bench_support import build_agent, load_env  # noqa: E402
from scripts.consolidation.s3_dry_run_kit import (  # noqa: E402
    MEMORY_DIR,
    SNAPSHOT_DIR,
    NoWriteFactRepository,
    RecordingCounters,
    SnapshotBlueprintOverlay,
    SnapshotProfileOverlay,
    SnapshotTokenOverlay,
    StatsSink,
    inside_memory_dir,
    owner_timezone,
)
from src.adapters.fact_management_adapter import FactManagementAdapter  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage, AgentStatus  # noqa: E402
from src.domain.billing import calculate_cost  # noqa: E402
from src.domain.consolidation import INBOX_TRIAGE_PROFILE_ID, TRIAGE_AGENT_TASK, consolidation_agent_id  # noqa: E402
from src.domain.memory_inbox import (  # noqa: E402
    ANSWER_WINDOW,
    TRIAGE_BATCH,
    Basis,
    Candidate,
    CandidateStatus,
    DraftClass,
    Relation,
    RelationKind,
    SourceCopy,
    Submission,
    SubmissionStatus,
)
from src.domain.memory_inbox_triage_task import TriageTask, decode_triage_result, encode_triage_task  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402
from src.services.fact_write_service import FactWriteService  # noqa: E402

TRIAGE_TOKEN_IDS: Tuple[str, ...] = ("CONSOLIDATION_INBOX_TRIAGE",)
TRIAGE_BLUEPRINT_ID = "consolidation_inbox_triage_v1"
DEFAULT_SUBMISSIONS = MEMORY_DIR / "inbox_triage_submissions.json"
DEFAULT_REPORT = MEMORY_DIR / "inbox_triage_dry_run_report.json"


class SpendCapReached(RuntimeError):
    """The next model call could pass `--max-usd` (or its model has no price): the run stops before it."""


class RunMeter:
    """Prices every model call of this run with its token legs and keeps the run under `max_usd`."""

    def __init__(self, max_usd: float) -> None:
        self.max_usd = max_usd
        self.calls: List[Dict[str, Any]] = []

    @property
    def usd(self) -> float:
        return round(sum(c["usd"] for c in self.calls), 6)

    def check(self, model: str) -> None:
        if calculate_cost(model, 1_000_000, 1_000_000) == 0.0:
            raise SpendCapReached(f"model {model!r} has no price in domain.billing: the cap cannot be kept")
        largest = max((c["usd"] for c in self.calls), default=0.0)
        if self.usd + largest > self.max_usd:
            raise SpendCapReached(f"spent ${self.usd:.4f} (largest call ${largest:.4f}); the next call could pass "
                                  f"--max-usd {self.max_usd:g}")

    def record(self, model: str, response: Any) -> None:
        usage = getattr(response, "usage_metadata", None)
        legs = {name: int(getattr(usage, name, 0) or 0) for name in
                ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_creation_tokens")}
        usd = calculate_cost(model, legs["prompt_tokens"], legs["completion_tokens"], legs["cache_read_tokens"],
                             legs["cache_creation_tokens"]) if usage is not None else 0.0
        self.calls.append({"model": model, "usd": round(usd, 6), "metered": usage is not None, **legs})
        print(f"    call {len(self.calls)}: {model} in={legs['prompt_tokens']} out={legs['completion_tokens']} "
              f"cache_read={legs['cache_read_tokens']} cache_write={legs['cache_creation_tokens']} ${usd:.4f} "
              f"(run ${self.usd:.4f} of ${self.max_usd:g})")

    def wrap(self, agent: Any) -> None:
        inner = agent._call_llm

        async def metered(request: Any, *args: Any, **kwargs: Any) -> Any:
            self.check(request.model_name)
            response = await inner(request, *args, **kwargs)
            self.record(request.model_name, response)
            return response

        agent._call_llm = metered

    def totals(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"usd": self.usd, "calls": len(self.calls),
                               "unmetered_calls": sum(1 for c in self.calls if not c["metered"])}
        for leg in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_creation_tokens"):
            out[leg] = sum(c[leg] for c in self.calls)
        return out


# ------------------------------------------------------------------------------------------------ input


def _basis(value: Any) -> Basis:
    return Basis(value) if value in {b.value for b in Basis} else Basis.INFERRED


def load_task(path: Path, now: datetime, language: Optional[str]) -> TriageTask:
    """The synthetic task: submissions received `received_hours_ago` before `now`, the waiting and answered blocks."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    submissions = tuple(
        Submission(submission_id=item["id"], user_id="dry-run", account_id="dry-run", status=SubmissionStatus.RECEIVED,
                   received_at=now - timedelta(hours=float(item.get("received_hours_ago", 0))), client_id="dry-run",
                   client_name=item.get("client", "Claude"), information=item["information"],
                   basis=_basis(item.get("basis")), said_when=item.get("said_when", ""),
                   happens_when=item.get("happens_when", ""))
        for item in raw.get("submissions", []))
    if not submissions or len(submissions) > TRIAGE_BATCH:
        raise ValueError(f"the input needs 1 to {TRIAGE_BATCH} submissions (one pass), got {len(submissions)}")
    waiting = tuple(_candidate(item["id"], item["text"], CandidateStatus.CANDIDATE, now)
                    for item in raw.get("waiting", []))
    answered = tuple(_candidate(f"a{n}", item["text"], CandidateStatus(item["answered"]), now)
                     for n, item in enumerate(raw.get("answered", []), 1))
    return TriageTask(submissions=submissions, waiting=waiting, answered=answered,
                      language=language or raw.get("language") or "en")


def _candidate(candidate_id: str, text: str, status: CandidateStatus, now: datetime) -> Candidate:
    created = now - timedelta(days=3)
    source = SourceCopy(submission_id=f"src-{candidate_id}", received_at=created, client_name="Claude",
                        information=text, basis=Basis.SAID, said_when="", happens_when="")
    return Candidate(candidate_id=candidate_id, user_id="dry-run", account_id="dry-run", status=status,
                     created_at=created, answer_by=created + ANSWER_WINDOW, language="en",
                     source_ids=(source.submission_id,), sources=(source,), text=text, happens_when="", said_when="",
                     basis=Basis.SAID, draft=DraftClass(directive=False, temporal_class="dynamic",
                                                        context_priority="medium", expiry={"kind": "none"}),
                     relation=Relation(RelationKind.NEW, None, None))


# ------------------------------------------------------------------------------------------------ the agent


def install(agent: Any, from_firestore: bool) -> Tuple[NoWriteFactRepository, StatsSink]:
    """The kit's no-write fact store, recording counters and sink, and the snapshot prompt pieces under `agent`."""
    service = getattr(getattr(agent, "prompt_builder", None), "assembly_service", None)
    if service is None:
        raise RuntimeError("the agent has no prompt assembly service: the snapshot token could not be served")
    live = agent._fact_management
    repo = NoWriteFactRepository(agent._repo)
    agent._repo = repo
    agent._bio_cache = repo
    agent._fact_management = FactManagementAdapter(repo, live._embedding, FactWriteService(live._embedding),
                                                   live._search_enrichment)
    agent._lifecycle_counters = RecordingCounters()
    sink = StatsSink()
    agent._alert_sink = sink
    agent._stats_alerts = True
    service.token_repo = SnapshotTokenOverlay(service.token_repo, service.security_port, SNAPSHOT_DIR, TRIAGE_TOKEN_IDS)
    if not from_firestore:
        service.profile_repo = SnapshotProfileOverlay(service.profile_repo, SNAPSHOT_DIR, (INBOX_TRIAGE_PROFILE_ID,))
        service.blueprint_repo = SnapshotBlueprintOverlay(service.blueprint_repo, SNAPSHOT_DIR, (TRIAGE_BLUEPRINT_ID,))
    service.invalidate_cache()
    return repo, sink


async def check_prompt(agent: Any, user_id: str, account_id: str) -> int:
    """The triage prompt builds and holds the taxonomy and the triage rule (no model call); its length."""
    prompt = await agent.prompt_builder.build_for_agent(
        agent_type=INBOX_TRIAGE_PROFILE_ID, user_id=user_id, account_id=account_id, routing_metadata=None,
        include_directives=False, biographical_facts=[], conversation_history=[])
    missing = [marker for marker in ("fact_taxonomy", "Inbox_Triage") if marker not in (prompt or "")]
    if missing:
        raise RuntimeError(f"the triage prompt lacks {', '.join(missing)}: check the profile and the blueprint")
    return len(prompt)


def report_rows(task: TriageTask, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per submission: its decision, and each candidate citing it with its text, language, time fields and relation."""
    decisions, ended, failed = decode_triage_result(result)
    rows = []
    for submission in task.submissions:
        sid = submission.submission_id
        cited = [c for c in decisions.candidates if sid in c.source_ids]
        rows.append({
            "id": sid, "information": submission.information,
            "decision": "cited" if cited else (decisions.discards[sid].value if sid in decisions.discards
                                               else "undecided"),
            "candidates": [{"text": c.fields.text, "language": task.language, "happens_when": c.fields.happens_when,
                            "said_when": c.fields.said_when, "basis": c.fields.basis.value,
                            "relation": c.relation.kind.value, "fact_id": c.relation.fact_id,
                            "draft": c.draft.to_record(), "cites": list(c.source_ids)} for c in cited],
        })
    return rows


async def main(args: argparse.Namespace) -> None:
    submissions_path = inside_memory_dir(Path(args.submissions))
    report_path = inside_memory_dir(Path(args.report))
    handles = await build_agent(args.user_id)
    agent = handles.agent
    zone = await owner_timezone(handles.account_repo, handles.user_repo, args.account_id, args.timezone)
    now = datetime.now(timezone.utc)
    task = load_task(submissions_path, now, args.language)
    install(agent, args.profile_from_firestore)
    meter = RunMeter(args.max_usd)
    meter.wrap(agent)
    if args.probe:
        agent.MAX_CONSOLIDATION_TURNS = 1
    prompt_chars = await check_prompt(agent, args.user_id, args.account_id)
    print(f"  triage prompt {prompt_chars} chars, zone {zone}, language {task.language}, "
          f"{len(task.submissions)} submissions, {len(task.waiting)} waiting, {len(task.answered)} answered")

    message = AgentMessage.create(
        sender="inbox_triage_dry_run", recipient=consolidation_agent_id(args.user_id), intent=AgentIntent.DELEGATE,
        payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(task)},
        context={"user_id": args.user_id, "account_id": args.account_id, "owner_timezone": zone},
    )
    report: Dict[str, Any] = {"started": now.isoformat(), "zone": zone, "language": task.language,
                              "probe": args.probe, "prompt_chars": prompt_chars}
    try:
        async with RequestContext(user_id=args.user_id, account_id=args.account_id):
            response = await agent.execute(message)
        report["status"] = response.status.value
        if response.status is AgentStatus.SUCCESS:
            _, ended, failed = decode_triage_result(response.result)
            report.update(ended=ended, failed=failed, rows=report_rows(task, response.result))
        else:
            report["error"] = response.error
    finally:
        report["cost"] = {"calls": meter.calls, "totals": meter.totals()}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for row in report.get("rows", []):
        print(f"\n  {row['id']} -> {row['decision']}")
        for c in row["candidates"]:
            print(f"     [{c['relation']}] {c['text']} | happens: {c['happens_when'] or '-'} | said: "
                  f"{c['said_when'] or '-'} | {c['basis']}")
    totals = meter.totals()
    print(f"\n  status={report.get('status')} ended={report.get('ended')} failed={report.get('failed')}")
    print(f"  cost ${totals['usd']:.4f} over {totals['calls']} calls: in={totals['prompt_tokens']} "
          f"out={totals['completion_tokens']} cache_read={totals['cache_read_tokens']} "
          f"cache_write={totals['cache_creation_tokens']} unmetered={totals['unmetered_calls']}")
    print(f"  report -> {report_path}")


if __name__ == "__main__":
    load_env()
    parser = argparse.ArgumentParser(description="Memory inbox triage dry run (PAID: calls the consolidation model)")
    parser.add_argument("--submissions", default=str(DEFAULT_SUBMISSIONS),
                        help="the synthetic input JSON under scripts/memory/")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="the report JSON under scripts/memory/")
    parser.add_argument("--max-usd", type=float, required=True,
                        help="hard stop: no model call that could take this run's spend past it")
    parser.add_argument("--language", default=None, help="the owner's UI language code (default: the input's, else en)")
    parser.add_argument("--timezone", default=None, help="IANA zone (default: the account owner's)")
    parser.add_argument("--probe", action="store_true", help="stop after the first model call (the estimate's probe)")
    parser.add_argument("--profile-from-firestore", action="store_true",
                        help="read the uploaded triage profile and blueprint instead of the snapshot's")
    parser.add_argument("--user-id", default=os.getenv("DEV_USER_ID"), help="default: DEV_USER_ID")
    parser.add_argument("--account-id", default=os.getenv("DEV_ACCOUNT_ID"), help="default: DEV_ACCOUNT_ID")
    parsed = parser.parse_args()
    if not parsed.user_id or not parsed.account_id:
        parser.error("set DEV_USER_ID and DEV_ACCOUNT_ID in .env or pass --user-id and --account-id")
    if parsed.max_usd <= 0:
        parser.error("--max-usd must be above 0")
    asyncio.run(main(parsed))
