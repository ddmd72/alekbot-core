#!/usr/bin/env python3
"""
Facts S5, one-time classify: give every stored current fact without an expiry decision the expiry it should have had.

The owner runs it; apply only on his word. What it does:

  * Dry run (default): scans the live facts collection (reads only), checks the live CONSOLIDATION_TAXONOMY token against
    the snapshot in `prompts_snapshot/`, asks the live consolidation agent once per batch of 25 facts (one retry for the
    facts a first answer left unusable), and writes the owner's report and a replay plan under `scripts/memory/`.
    Nothing is written to the database.
  * `--apply PLAN`: reads a plan file and writes `expiry_kind`, `ttl_days`, `expires_at` and one `lifecycle_events` entry
    on each fact, through the repository port, under the change token recorded at the scan. A fact changed since the
    scan is skipped and counted; a second apply of the same plan changes nothing. No agent, no model.

A run without `--budget-usd` only estimates: no classify call is made (building an agent makes one embedding call).
`--expect-model` is required for a dry run: an account whose agent runs another model stops the run before the estimate.
The cap covers the recorded calls of ALL classify runs, with the two exceptions under Residuals: it is checked against
classify's own lifetime ledger, so a second run starts from what the first spent.

Flags:
    --expect-model NAME   the model every account's agent must run (dry run only, required)
    --budget-usd X        the lifetime spend cap; without it the run only estimates
    --ids-file PATH       classify only these fact ids (one per line, blank and # lines ignored; under scripts/memory/)
    --user-id ID          the user whose agent is built first (default: DEV_USER_ID from the dotenv file)
    --apply PLAN          apply a plan file (under scripts/memory/); no other flag is allowed with it

Files, all under the gitignored `scripts/memory/` (`<stamp>` is the run's UTC time):
    fact_s5_classify_<stamp>_report.md           the owner's table, with the fact text
    fact_s5_classify_<stamp>_plan.json           the replay plan
    fact_s5_classify_<stamp>_not_decided.txt     only when the run stopped: fact ids, the failing batch last
    fact_s5_classify_<stamp>_stopped_batch.txt   only when the run stopped: the failing batch's fact ids
    fact_s5_classify_<stamp>_applied.json        apply: ids and outcomes, no text
    fact_s5_classify_<stamp>_changed.txt         apply: ids of facts changed since the scan
    fact_s5_classify_spend.json                  classify's own cumulative spend ledger

stdout prints counts and file paths only: never fact text, fact ids or account ids.

Residuals:
  * With DEBUG_PROMPTS=true the agent writes `prompt_content` rows for each call.
  * Spend is kept only in classify's own ledger, not in the user's billing counters.
  * On a claude-sonnet-5-5 pin the Claude adapter's same-provider fallback can answer a call with claude-sonnet-5 on a 5xx,
    unnoticed here, priced at the 5.5 rate; the live consolidator behaves the same.
  * In an account with several users the first owner's agent config is used for the whole account.
  * A call that raises is not recorded by the meter (it records only returned calls), so its cost is not in the ledger.
  * The meter's per-call check lets one call run past the cap: the call that crosses it is still made and recorded.

Usage:
    python -m scripts.migration.fact_s5_classify --expect-model NAME [--budget-usd X] [--ids-file PATH] [--user-id ID]
    python -m scripts.migration.fact_s5_classify --apply PLAN
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AbstractSet, Any, Awaitable, Callable, Dict, Final, List, Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

from scripts.consolidation.s3_bench_support import build_agent, load_env  # noqa: E402
from scripts.consolidation.s3_dry_run_kit import BudgetExceeded, CostMeter, _serializer, inside_memory_dir  # noqa: E402
from scripts.migration.fact_s5_classify_rules import (  # noqa: E402
    BATCH_SIZE,
    FALLBACK_MISSING,
    OUTCOME_ALREADY,
    OUTCOME_APPLIED,
    OUTCOME_CHANGED,
    OUTCOME_FAILED,
    RESPONSE_SCHEMA,
    SKIP_OWNER,
    Decision,
    PlanFileError,
    ScannedFact,
    Skipped,
    already_applied,
    apply_fields,
    batches,
    estimate_usd,
    extract_blocks,
    fact_line,
    fallback_decision,
    is_pinned,
    judge,
    pinned_decision,
    plan_document,
    plan_entry,
    planned_fields,
    read_answer,
    read_plan,
    render_report,
    report_row,
    scanned_fact,
    select,
    summary,
    summary_lines,
    system_prompt,
    user_message,
)
from src.config.environment import EnvironmentConfig  # noqa: E402
from src.domain.entities import FactState  # noqa: E402
from src.domain.exceptions import FactResolutionError, FactWriteConflict  # noqa: E402
from src.domain.llm import LLMRequest, LLMResponse, Message, MessagePart  # noqa: E402
from src.domain.owner_timezone import ResolvedOwnerZone  # noqa: E402
from src.domain.request_context import RequestContext  # noqa: E402
from src.infrastructure.agent_config import CONSOLIDATION  # noqa: E402
from src.services.owner_timezone_resolver import OwnerTimezoneResolver  # noqa: E402

MEMORY_DIR = PROJECT_ROOT / "scripts" / "memory"          # outputs are still checked with the kit's inside_memory_dir
SPEND_LEDGER = MEMORY_DIR / "fact_s5_classify_spend.json"
INSTRUCTION_FILE = Path(__file__).with_name("fact_s5_classify_instruction.txt")
TAXONOMY_SNAPSHOT = PROJECT_ROOT / "prompts_snapshot" / "tokens" / "system" / "CONSOLIDATION_TAXONOMY.groovy"
TAXONOMY_TOKEN_ID = "CONSOLIDATION_TAXONOMY"
SCAN_FIELDS = ["account_id", "text", "domain", "type", "tags", "temporal_class", "metadata", "context",
               "reported_date", "created_at", "expiry_kind"]
REQUEST_TIMEOUT_S: Final[int] = 500

RESIDUALS: Final[Tuple[str, ...]] = (
    "On a claude-sonnet-5-5 pin the Claude adapter's same-provider fallback can answer a call with claude-sonnet-5 on a "
    "5xx, unnoticed here, priced at the 5.5 rate; the live consolidator behaves the same.",
    "In an account with several users the first owner's agent config is used for the whole account.",
    "A call that raises is not recorded by the meter (it records only returned calls), so its cost is not in the ledger.",
    "The meter's per-call check lets one call run past the cap: the call that crosses it is still made and recorded.",
)


@dataclass(frozen=True)
class ModelCaller:
    model: str
    call: Callable[[LLMRequest], Awaitable[LLMResponse]]


@dataclass
class AccountWork:
    account_id: str
    owner_user_id: str
    timezone: str
    pinned: List[ScannedFact]
    for_model: List[ScannedFact]


@dataclass
class Prepared:
    scanned: int
    accounts: List[AccountWork]
    skipped: List[Skipped]


class PromptDrift(RuntimeError):
    """The live taxonomy token is not the snapshot this script reads."""


class ModelMismatch(RuntimeError):
    """An account's agent does not run the model the owner expects."""


class RunStopped(RuntimeError):
    """The run stops safely; `reason` is what the report and the plan say."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------------------------------------------------
# Scan, prompt, preparation
# ---------------------------------------------------------------------------------------------------------------------


async def scan(db: Any, collection: str) -> List[ScannedFact]:
    """Every current fact, with the fields classify reads and its change token (reads only)."""
    query = db.collection(collection).where(filter=FieldFilter("state", "==", FactState.CURRENT.value)).select(SCAN_FIELDS)
    facts: List[ScannedFact] = []
    async for snap in query.stream():
        facts.append(scanned_fact(snap.id, snap.to_dict() or {}, snap.update_time))
    return facts


def load_prompt(snapshot_text: str, instruction_text: str, live_file_text: str) -> Tuple[str, str, str]:
    """(system prompt, taxonomy digest, instruction digest). The live token must be the snapshot, character for character."""
    if live_file_text != snapshot_text:
        raise PromptDrift("live CONSOLIDATION_TAXONOMY differs from the snapshot: run snapshot_pull --check")
    content = _serializer().token_from_file(snapshot_text)["content"]
    blocks = extract_blocks(content)
    return (
        system_prompt(blocks, instruction_text),
        hashlib.sha256(blocks.encode("utf-8")).hexdigest(),
        hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
    )


async def live_taxonomy_file(db: Any, tokens_system_collection: str) -> str:
    """The live taxonomy token rendered exactly as `snapshot_pull` renders a system token."""
    snap = await db.collection(tokens_system_collection).document(TAXONOMY_TOKEN_ID).get()
    if not snap.exists:
        raise PromptDrift("the live CONSOLIDATION_TAXONOMY token does not exist")
    text: str = _serializer().token_to_file(snap.to_dict())
    return text


async def prepare(
    db: Any,
    collection: str,
    owner_of: Callable[[str], Awaitable[ResolvedOwnerZone]],
    ids: Optional[AbstractSet[str]] = None,
) -> Prepared:
    facts = await scan(db, collection)
    selected, skipped = select(facts, ids)
    by_account: Dict[str, List[ScannedFact]] = {}
    for fact in selected:
        by_account.setdefault(fact.account_id, []).append(fact)
    accounts: List[AccountWork] = []
    for account_id, group in by_account.items():
        resolved = await owner_of(account_id)
        if resolved.owner_user_id is None:
            skipped.extend(Skipped(f.fact_id, SKIP_OWNER) for f in group)
            continue
        accounts.append(AccountWork(
            account_id=account_id,
            owner_user_id=resolved.owner_user_id,
            timezone=resolved.zone,
            pinned=[f for f in group if is_pinned(f)],
            for_model=[f for f in group if not is_pinned(f)],
        ))
    return Prepared(len(facts), accounts, skipped)


# ---------------------------------------------------------------------------------------------------------------------
# Models, estimate, cap
# ---------------------------------------------------------------------------------------------------------------------


def check_models(models: Mapping[str, str], expected: str) -> None:
    wrong = [f"{account[:8]}: {model} (expected {expected})" for account, model in models.items() if model != expected]
    if wrong:
        raise ModelMismatch("an account's agent runs another model than --expect-model: " + "; ".join(wrong))


def _calls_of(account: AccountWork, system: str) -> List[Tuple[int, int]]:
    calls: List[Tuple[int, int]] = []
    for batch in batches(account.for_model):
        lines = [fact_line(n, fact, account.timezone) for n, fact in enumerate(batch, 1)]
        calls.append((len(system) + len(user_message(lines)), len(batch)))
    return calls


def estimate_lines(
    prepared: Prepared, models: Mapping[str, str], system: str, spent_usd: Optional[float] = None
) -> Tuple[List[str], Optional[Tuple[float, float]]]:
    out: List[str] = []
    low_total = 0.0
    high_total = 0.0
    priced = True
    for account in prepared.accounts:
        model = models[account.account_id]
        calls = _calls_of(account, system)
        estimate = estimate_usd(model, calls)
        if estimate is None:
            priced = False
            cost = "unpriced model"
        else:
            low_total += estimate[0]
            high_total += estimate[1]
            cost = f"${estimate[0]:.2f}-${estimate[1]:.2f}"
        out.append(
            f"account {account.account_id[:8]}: model {model}, owner zone {account.timezone}, "
            f"{len(account.for_model)} facts, {len(calls)} batches, {cost}"
        )
    total = (low_total, high_total) if priced else None
    out.append(f"total: ${low_total:.2f}-${high_total:.2f}" if total is not None else "total: unpriced")
    if spent_usd is not None:
        out.append(f"spent so far (all classify runs): ${spent_usd:.4f}")
    return out, total


def cap_verdict(spent_usd: float, estimate: Tuple[float, float], budget_usd: float) -> Tuple[bool, Optional[str]]:
    """(refuse, warning): refuse when the lifetime spend plus the low estimate is above the cap."""
    low, high = estimate
    refuse = spent_usd + low > budget_usd
    warning = None
    if spent_usd + high > budget_usd:
        warning = f"spent so far ${spent_usd:.2f} + estimate high ${high:.2f} may pass the cap ${budget_usd:.2f}"
    return refuse, warning


def wrap_once(meter: Any, agents: Sequence[Any]) -> None:
    """Meter each distinct agent object once, so two accounts of one owner never stack two wrappers."""
    wrapped: Dict[int, Any] = {}
    for agent in agents:
        if id(agent) not in wrapped:
            wrapped[id(agent)] = agent
            meter.wrap(agent)


def model_caller(agent: Any, owner_user_id: str, account_id: str) -> ModelCaller:
    async def call(request: LLMRequest) -> LLMResponse:
        async with RequestContext(user_id=owner_user_id, account_id=account_id):
            response: LLMResponse = await agent._call_llm(request)  # looked up now: a meter installed meanwhile is used
            return response

    return ModelCaller(agent.model_name, call)


# ---------------------------------------------------------------------------------------------------------------------
# The dry run
# ---------------------------------------------------------------------------------------------------------------------


def _request(caller: ModelCaller, system: str, lines: Sequence[Mapping[str, Any]], problems: Optional[Mapping[int, str]]) -> LLMRequest:
    return LLMRequest(
        model_name=caller.model,
        system_instruction=system,
        messages=[Message(role="user", parts=[MessagePart(text=user_message(lines, problems))])],
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        max_tokens=CONSOLIDATION.max_tokens,
        thinking=CONSOLIDATION.thinking_effort or None,
        timeout=REQUEST_TIMEOUT_S,
    )


async def _ask(
    caller: ModelCaller, system: str, lines: Sequence[Mapping[str, Any]], problems: Optional[Mapping[int, str]]
) -> Optional[str]:
    """One call, repeated once on an error. Raises RunStopped for a second error, a spend cap or a fallback answer."""
    request = _request(caller, system, lines, problems)
    for attempt in (1, 2):
        try:
            response = await caller.call(request)
        except BudgetExceeded:
            raise RunStopped("spend cap") from None
        except Exception as exc:
            if attempt == 2:
                raise RunStopped(f"call failed twice: {type(exc).__name__}") from None
            continue
        if response.served_by_fallback is not None:
            raise RunStopped(f"answered by the fallback provider {response.served_by_fallback}")
        return response.text
    raise AssertionError("unreachable")  # pragma: no cover


async def _classify_batch(
    batch: Sequence[ScannedFact], caller: ModelCaller, system: str, zone: str, decisions: Dict[str, Decision]
) -> None:
    """Decide one batch into `decisions`. A stop inside leaves the facts still undecided out of it."""
    numbered = dict(enumerate(batch, 1))
    lines = {n: fact_line(n, fact, zone) for n, fact in numbered.items()}
    answered, problems = read_answer(await _ask(caller, system, [lines[n] for n in numbered], None), set(numbered))
    pending: Dict[int, str] = {}
    for n, fact in numbered.items():
        raw = answered.get(n)
        if raw is None:
            pending[n] = problems.get(n, FALLBACK_MISSING)
            continue
        decision, code = judge(fact, raw, zone)
        if decision is not None:
            decisions[fact.fact_id] = decision
        else:
            pending[n] = str(code)
    if not pending:
        return
    retry = [lines[n] for n in sorted(pending)]
    answered, _ = read_answer(await _ask(caller, system, retry, pending), set(pending))
    for n in sorted(pending):
        fact = numbered[n]
        decision, code = judge(fact, answered.get(n), zone)
        decisions[fact.fact_id] = decision if decision is not None else fallback_decision(fact, str(code))


def _write(out_dir: Path, name: str, text: str) -> Path:
    path = inside_memory_dir(out_dir / name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _lines_file(ids: Sequence[str]) -> str:
    return "".join(f"{fact_id}\n" for fact_id in ids)


async def classify(
    prepared: Prepared,
    callers: Mapping[str, ModelCaller],
    system: str,
    digests: Tuple[str, str],
    *,
    collection: str,
    out_dir: Path,
    now: datetime,
    meter: Any = None,
) -> Dict[str, Any]:
    decisions: Dict[str, Decision] = {}
    for account in prepared.accounts:
        for fact in account.pinned:
            decisions[fact.fact_id] = pinned_decision(fact)
    stopped: Optional[str] = None
    later: List[str] = []
    failing: List[str] = []
    for account in prepared.accounts:
        for batch in batches(account.for_model, BATCH_SIZE):
            if stopped is not None:
                later.extend(f.fact_id for f in batch)
                continue
            try:
                await _classify_batch(batch, callers[account.account_id], system, account.timezone, decisions)
            except RunStopped as stop:
                stopped = stop.reason
                failing = [f.fact_id for f in batch if f.fact_id not in decisions]
    not_decided = later + failing

    entries = []
    rows = []
    ordered: List[Decision] = []
    fields_by_id: Dict[str, Dict[str, Any]] = {}
    zones: Dict[str, str] = {}
    no_reported_date = 0
    for account in prepared.accounts:
        zones[account.account_id[:8]] = account.timezone
        for fact in [*account.pinned, *account.for_model]:
            no_reported_date += 1 if fact.reported_date is None else 0
            decision = decisions.get(fact.fact_id)
            if decision is None:
                continue
            fields = planned_fields(decision, fact, account.timezone)
            fields_by_id[fact.fact_id] = fields
            ordered.append(decision)
            entries.append(plan_entry(decision, fact, fields))
            rows.append(report_row(fact, decision, fields, account.timezone, now))

    spend = {"run_usd": meter.run_usd, "spent_usd": meter.spent_usd} if meter is not None else None
    result = summary(ordered, fields_by_id, prepared.skipped, not_decided, now, spend, zones, no_reported_date)
    result["stopped"] = stopped
    lines = summary_lines(result)
    if stopped is not None:
        lines.append(f"stopped: {stopped}")
    lines.extend(RESIDUALS)

    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"fact_s5_classify_{stamp}"
    models = {account_id: caller.model for account_id, caller in callers.items()}
    plan = plan_document(
        entries, prepared.skipped, not_decided, collection=collection, models=models, generated_at=now,
        taxonomy_sha256=digests[0], instruction_sha256=digests[1], stopped=stopped,
    )
    paths = [
        _write(out_dir, f"{prefix}_report.md", render_report(rows, lines)),
        _write(out_dir, f"{prefix}_plan.json", json.dumps(plan, indent=2, ensure_ascii=False)),
    ]
    if stopped is not None:
        paths.append(_write(out_dir, f"{prefix}_not_decided.txt", _lines_file(not_decided)))
        paths.append(_write(out_dir, f"{prefix}_stopped_batch.txt", _lines_file(failing)))

    print(f"scanned: {prepared.scanned}")
    for line in lines:
        print(line)
    for path in paths:
        print(f"file: {path}")
    return result


# ---------------------------------------------------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------------------------------------------------


async def apply_plan(repo: Any, plan_path: Path, collection: str, out_dir: Path, now: datetime) -> Dict[str, int]:
    path = inside_memory_dir(plan_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise PlanFileError("the plan file is not JSON") from None
    entries = read_plan(document, collection)  # the whole plan is validated before any fact is read
    outcomes: Dict[str, str] = {}
    for entry in entries:
        outcomes[entry.fact_id] = await _apply_entry(repo, entry, now)
    counts = {name: 0 for name in (OUTCOME_APPLIED, OUTCOME_ALREADY, OUTCOME_CHANGED, OUTCOME_FAILED)}
    for outcome in outcomes.values():
        counts[outcome] += 1

    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"fact_s5_classify_{stamp}"
    report = {"generated_at": now.isoformat(), "plan": path.name, "outcomes": outcomes}
    paths = [_write(out_dir, f"{prefix}_applied.json", json.dumps(report, indent=2))]
    changed = [fact_id for fact_id, outcome in outcomes.items() if outcome == OUTCOME_CHANGED]
    if changed:
        paths.append(_write(out_dir, f"{prefix}_changed.txt", _lines_file(changed)))
    for name, value in counts.items():
        print(f"{name}: {value}")
    for written in paths:
        print(f"file: {written}")
    return counts


async def _apply_entry(repo: Any, entry: Any, now: datetime) -> str:
    """The outcome of one entry. Never raises: the loop must reach the files."""
    try:
        try:
            stored = await repo.resolve_current(entry.fact_id, entry.account_id, frozenset({FactState.CURRENT}))
        except FactResolutionError:
            return OUTCOME_CHANGED
        if stored is None or stored.fact.id != entry.fact_id:
            return OUTCOME_CHANGED
        if str(stored.update_time) != entry.token:
            return OUTCOME_ALREADY if already_applied(entry, stored.fact) else OUTCOME_CHANGED
        try:
            await repo.update_fact_fields(stored, apply_fields(entry, stored.fact.lifecycle_events, now))
        except FactWriteConflict:
            return OUTCOME_CHANGED
        return OUTCOME_APPLIED
    except Exception as exc:  # the type only: a message may carry document values
        print(f"  entry failed: {type(exc).__name__}")
        return OUTCOME_FAILED


# ---------------------------------------------------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Built after `load_env()`, so the DEV_USER_ID default is read from the dotenv file."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expect-model", metavar="NAME", help="the model every account's agent must run (dry run, required)")
    parser.add_argument("--budget-usd", type=float, help="lifetime spend cap over all classify runs; omitted: estimate only")
    parser.add_argument("--ids-file", metavar="PATH", help="classify only the fact ids listed in this file")
    parser.add_argument("--apply", metavar="PLAN", help="apply a plan file instead of classifying")
    parser.add_argument("--user-id", default=os.getenv("DEV_USER_ID"), help="the user whose agent is built first")
    return parser


def _read_ids(path_text: str) -> AbstractSet[str]:
    path = inside_memory_dir(Path(path_text))
    ids = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    return {i for i in ids if i and not i.startswith("#")}


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply is not None:
        if args.budget_usd is not None or args.ids_file is not None or args.expect_model is not None:
            parser.error("--apply takes no --budget-usd, --ids-file or --expect-model")
    else:
        if not args.expect_model:
            parser.error("a dry run needs --expect-model")
        if not args.user_id:
            parser.error("set DEV_USER_ID in the dotenv file or pass --user-id")
    ids = _read_ids(args.ids_file) if args.ids_file is not None else None
    env_config = EnvironmentConfig()
    collection = env_config.domain_facts_collection
    tokens_system_collection = env_config.domain_prompt_tokens_collection + "_system"

    if args.apply is not None:
        return asyncio.run(_apply_main(args, collection, env_config))
    return asyncio.run(_dry_run_main(args, collection, tokens_system_collection, ids))


async def _apply_main(args: argparse.Namespace, collection: str, env_config: Any) -> int:
    from scripts._firestore import async_client   # loads .env at import: only when run for real
    from src.adapters.firestore_repo import FirestoreFactRepository

    repo = FirestoreFactRepository(async_client(), env_config)
    counts = await apply_plan(repo, Path(args.apply), collection, MEMORY_DIR, datetime.now(timezone.utc))
    return 1 if counts[OUTCOME_FAILED] else 0


async def _dry_run_main(
    args: argparse.Namespace, collection: str, tokens_system_collection: str, ids: Optional[AbstractSet[str]]
) -> int:
    handles = await build_agent(args.user_id)
    owner_of = OwnerTimezoneResolver(handles.account_repo, handles.user_repo).resolve
    prepared = await prepare(handles.db, collection, owner_of, ids)

    agents: Dict[str, Any] = {args.user_id: handles.agent}
    for account in prepared.accounts:
        if account.owner_user_id not in agents:
            agents[account.owner_user_id] = (await build_agent(account.owner_user_id)).agent
    models = {a.account_id: agents[a.owner_user_id].model_name for a in prepared.accounts}
    try:
        check_models(models, args.expect_model)
        system, taxonomy_digest, instruction_digest = load_prompt(
            TAXONOMY_SNAPSHOT.read_text(encoding="utf-8"),
            INSTRUCTION_FILE.read_text(encoding="utf-8"),
            await live_taxonomy_file(handles.db, tokens_system_collection),
        )
    except (ModelMismatch, PromptDrift) as problem:
        print(str(problem))
        return 1

    if args.budget_usd is None:
        for line in estimate_lines(prepared, models, system)[0]:
            print(line)
        print("no cap given: no classify call was made (one embedding call per agent build)")
        return 0

    meter = CostMeter(SPEND_LEDGER, args.budget_usd)
    try:
        lines, total = estimate_lines(prepared, models, system, spent_usd=meter.spent_usd)
        for line in lines:
            print(line)
        if total is None:
            print("a model has no price: the cap cannot be kept; nothing was run")
            return 1
        refuse, warning = cap_verdict(meter.spent_usd, total, args.budget_usd)
        if refuse:
            print(
                f"spent so far ${meter.spent_usd:.2f} + estimate low ${total[0]:.2f} is above the cap "
                f"${args.budget_usd:.2f} (the cap covers all classify runs)"
            )
            return 1
        if warning is not None:
            print(warning)
        wrap_once(meter, list(agents.values()))
        callers = {a.account_id: model_caller(agents[a.owner_user_id], a.owner_user_id, a.account_id) for a in prepared.accounts}
        result = await classify(
            prepared, callers, system, (taxonomy_digest, instruction_digest),
            collection=collection, out_dir=MEMORY_DIR, now=datetime.now(timezone.utc), meter=meter,
        )
        return 1 if result["stopped"] else 0
    finally:
        meter.close()


if __name__ == "__main__":
    raise SystemExit(main())
