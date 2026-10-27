"""
Shared pieces of the five benches' `--s3-kit` mode (Facts S3, plan T12b).

With `--s3-kit` a bench stops simulating the consolidation passes and runs the pass its gate reads, on the dry-run kit
(`s3_dry_run_kit.py`): the real agent loop, parser and `FactManagementAdapter` over a no-write store. This module holds
what the five benches share: the flags, the agent build, the bench message, the explicit cluster of a `consolidate_cluster`
pass, the report and the gate lines. Nothing here builds a client or reads `.env` at import.
"""

import asyncio
import json
import os
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from scripts.consolidation import s3_dry_run_kit as kit_module
from scripts.consolidation.s3_dry_run_kit import DryRunKit, add_kit_arguments, check_budget_flag, inside_memory_dir
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.consolidation import consolidation_agent_id
from src.domain.entities import FactState
from src.domain.fact_tool_result import WRITTEN_STATUSES

G1_MAX_TURNS = 13
G1_PASSES = ("stage2a", "cluster")
CLUSTER_LIMIT = 30
SEARCH_LIMIT = 20

Search = Callable[..., Awaitable[List[Dict[str, Any]]]]


class KitRunFailed(RuntimeError):
    """A kit run cannot go on: the agent answered with a failure, or the agent is not what the kit needs."""


def add_s3_arguments(parser: Any) -> None:
    """`--s3-kit` and the kit's own flags (`--budget-usd`, `--allow-over-owner-cap`, `--timezone`); the flags do nothing without `--s3-kit`."""
    parser.add_argument("--s3-kit", action="store_true",
                        help="run the pass the S3 gate reads on the dry-run kit (no write reaches the live store)")
    add_kit_arguments(parser)


def load_env() -> None:
    """Read the dotenv file (the benches call this first thing in `__main__`, never at import).

    Values the `src` modules read once at import (`ENABLE_GROUNDING_ATTRIBUTION`, `DEEP_RESEARCH_SECOND_PASS` in
    `agent_config.py`, `LOG_TRACE_CONTEXT` in `logger.py`) no longer see the dotenv file in a bench run; none of them
    affects consolidation (the last only changes the console log format)."""
    from dotenv import load_dotenv

    load_dotenv()


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def kit_message(
    kit: DryRunKit, *, sender: str, user_id: str, account_id: str, task: str, payload: Dict[str, Any],
    session_id: str = "s3_bench",
) -> AgentMessage:
    """The message a bench sends the agent: the task the gate reads, the payload, and the owner's zone in the context."""
    return AgentMessage.create(
        sender=sender, recipient=consolidation_agent_id(user_id), intent=AgentIntent.DELEGATE,
        payload={"task": task, **payload}, context=kit.message_context(user_id, account_id, session_id=session_id),
    )


def require_success(response: AgentResponse, what: str) -> AgentResponse:
    """A failed response (a refused snapshot, an exhausted budget, an error in the pass) stops the bench."""
    if response.status != AgentStatus.SUCCESS:
        raise KitRunFailed(f"{what} did not succeed: {response.status.value}: {response.error}")
    return response


async def execute(agent: Any, message: AgentMessage, user_id: str, account_id: str, what: str) -> AgentResponse:
    from src.domain.request_context import RequestContext

    async with RequestContext(user_id=user_id, account_id=account_id):
        response = await agent.execute(message)
    return require_success(response, what)


# ------------------------------------------------------------------------------------------------ agent and kit

@dataclass
class BenchHandles:
    db: Any
    config: Dict[str, Any]
    container: Any
    account_repo: Any
    user_repo: Any
    agent: Any


async def build_agent(user_id: str) -> BenchHandles:
    """The consolidation agent as production builds it (live repositories, live counters), before the kit is installed."""
    from google.cloud import firestore

    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_user_repo import FirestoreUserRepository
    from src.composition.service_container import ServiceContainer
    from src.composition.user_agent_factory import UserAgentFactory
    from src.config.settings import load_settings
    from src.infrastructure.agent_coordinator import AgentCoordinator

    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    config = load_settings()
    env_config = config["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=config, db_client=db, env_config=env_config, account_repo=account_repo)
    factory = UserAgentFactory(
        config=config, env_config=env_config, coordinator=AgentCoordinator(),
        user_repo=user_repo, account_repo=account_repo, **container.agent_services(),
    )
    agents = await factory.ensure_agents_for_user(user_id)
    agent = agents.get("consolidation_agent")
    if agent is None:
        raise KitRunFailed(f"consolidation_agent not built; keys: {list(agents)}")
    if agent._fact_management is None:
        raise KitRunFailed("the agent was built without a fact-management port")
    return BenchHandles(db, config, container, account_repo, user_repo, agent)


async def biographical_facts(handles: BenchHandles, account_id: str) -> List[Dict[str, Any]]:
    try:
        return await handles.container.repository.get_biographical_context_cached(account_id, limit=100)
    except Exception as exc:
        print(f"  biographical context unavailable ({type(exc).__name__}); the agent reads its cache itself")
        return []


@asynccontextmanager
async def kit_session(
    handles: BenchHandles, args: Any, user_id: str, account_id: str, bench: str, *, with_review: bool = False,
    summary: Optional[Callable[[Dict[str, Any], Dict[str, Any]], List[str]]] = None,
    sidecar: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
) -> AsyncIterator[Tuple[DryRunKit, Dict[str, Any]]]:
    """The kit installed, prepared and, whatever happens in the body, reported and closed.

    One kit per process: the spend ledger is locked for the kit's life. The body adds what is bench-specific to the
    `extra` dict; the report goes to `scripts/memory/` even when the run stopped on a failed response or the cap.
    `sidecar(extra)` may return the text of a second, human file written beside the JSON.
    `with_review` makes the kit serve and check the memory review's token, profile and blueprint too; `summary(report,
    extra)` gives the gate lines printed at the end (default: the S3 gates)."""
    budget = check_budget_flag(args.budget_usd, getattr(args, "allow_over_owner_cap", False))
    kit = DryRunKit(ledger_path=kit_module.SPEND_LEDGER, budget_usd=budget, with_review=with_review)
    try:
        await kit.resolve_timezone(handles.account_repo, handles.user_repo, account_id, args.timezone)
        kit.install(handles.agent)
        await kit.prepare(user_id, account_id)
        print(f"  S3 kit: v3 snapshot tokens served, owner zone {kit.timezone}, "
              f"spent so far ${kit.meter.spent_usd:.2f} of ${kit.meter.budget_usd:.2f}")
        extra: Dict[str, Any] = {}
        try:
            yield kit, extra
        finally:
            path = write_bench_report(kit, bench, extra)
            print(f"\n  kit report -> {path}")
            text = sidecar(extra) if sidecar is not None else None
            if text:   # a human file beside the JSON: `<report name>_listed.md`
                md_path = kit_module.inside_memory_dir(path.with_name(f"{path.stem}_listed.md"))
                md_path.write_text(text, encoding="utf-8")
                print(f"  listed facts -> {md_path}")
            for line in (summary(kit.report(), extra) if summary is not None else gate_summary(kit.report())):
                print(f"  {line}")
    finally:
        kit.close()


# ------------------------------------------------------------------------------------------------ cluster

async def cluster_from_search(
    search: Search, contents: Sequence[str], seeds: Sequence[Dict[str, Any]], timezone: str, *,
    user_id: str, account_id: str, limit: int = CLUSTER_LIMIT,
) -> List[Dict[str, Any]]:
    """The explicit cluster of a `consolidate_cluster` pass, built from search dicts as Stage 2a builds its own.

    `seeds` (cluster entries of the facts under review) come first and are kept as they are; one content search per entry
    of `contents` adds what it finds, de-duplicated by id (the higher similarity wins), longest first, up to `limit` entries
    in all (never fewer than the seeds). The searches run in the request context of `user_id` / `account_id`, which the
    search service needs."""
    from src.domain.request_context import RequestContext

    async def one(content: str) -> List[Dict[str, Any]]:
        keywords = [w for w in content.split()[:10] if len(w) > 3]
        return await search(
            keywords=keywords, primary_query=content, alternative_query="", limit=SEARCH_LIMIT, timezone=timezone,
        )

    async with RequestContext(user_id=user_id, account_id=account_id):
        found = await asyncio.gather(*[one(c) for c in contents if c])
    seed_ids = {e["fact_id"] for e in seeds}
    others: Dict[str, Dict[str, Any]] = {}
    for results in found:
        for fact in results:
            fid = fact.get("fact_id")
            if not fid or fid in seed_ids:
                continue
            if fid not in others or (fact.get("similarity") or 0) > (others[fid].get("similarity") or 0):
                others[fid] = fact
    rest = sorted(others.values(), key=lambda f: len((f.get("content") or "").split()), reverse=True)
    return (list(seeds) + rest)[:max(limit, len(seeds))]


async def written_entries(kit: DryRunKit, first_row: int, timezone: str) -> List[Dict[str, Any]]:
    """Cluster entries of the facts the kit's writes since `first_row` left current (read back from the overlay)."""
    from src.agents.consolidation_agent import ConsolidationAgent

    entries: List[Dict[str, Any]] = []
    seen: set = set()
    for row in kit.rows[first_row:]:
        fact_id = row.get("fact_id")
        if not fact_id or fact_id in seen or row.get("status") not in WRITTEN_STATUSES:
            continue
        seen.add(fact_id)
        fact = await kit.repo.get_fact_by_id(fact_id) if kit.repo else None
        if fact is not None and fact.state is FactState.CURRENT:
            entries.append(ConsolidationAgent._cluster_entry(fact, timezone))
    return entries


# ------------------------------------------------------------------------------------------------ report

def print_rows(rows: Sequence[Dict[str, Any]]) -> None:
    """One line per write of the kit (ids and shapes, no fact text), then the totals."""
    counts = Counter((r["pass"], r["action"], r["status"]) for r in rows)
    for number, row in enumerate(rows, 1):
        decision = row.get("decision_stored") or {}
        print(f"  {number:3}. {row['pass']:8} {row['action']:7} {str(row['status']):8} "
              f"expiry sent={json.dumps(row['expiry_sent'], default=str)} "
              f"stored={decision.get('expiry_kind')}/{decision.get('ttl_days')} "
              f"date form={row['reported_date_form']} refused={row.get('refused')}")
    print(f"  writes: {sum(counts.values())}  " + "  ".join(f"{'/'.join(k)}:{v}" for k, v in sorted(counts.items())))


def gate_summary(report: Dict[str, Any]) -> List[str]:
    """The lines the owner reads against the gates: G1 turns of the `stage2a`/`cluster` passes, G2 abandoned refusals."""
    passes = report.get("passes", [])
    gated = [p for p in passes if p["pass"] in G1_PASSES]
    lines: List[str] = []
    if gated:
        worst = max(p["llm_calls"] for p in gated)
        verdict = "FAIL" if worst > G1_MAX_TURNS else "ok"
        lines.append(f"G1 {verdict}: {len(gated)} stage2a/cluster pass(es), most model turns {worst} (limit {G1_MAX_TURNS})")
    else:
        lines.append("G1: no stage2a or cluster pass ran in this run")
    abandoned = sum(p["abandoned_refusals"] for p in passes)
    lines.append(f"G2 {'FAIL' if abandoned else 'ok'}: {abandoned} abandoned refusal(s) in {len(passes)} pass(es)")
    lines.append("G3: read the write rows (expiry sent, decision stored) in the report")
    undated = report.get("cleanup_creates_without_derived_from", {"per_pass": {}, "total": 0})
    by_pass = ", ".join(f"{label} {count}" for label, count in sorted(undated["per_pass"].items())) or "none"
    lines.append(
        f"cleanup creates without derived_from: {undated['total']} ({by_pass}); their dates restart from the batch "
        "end, or from now on a cluster pass"
    )
    if report.get("spend", {}).get("aborted"):
        lines.append("RUN STOPPED by the spend meter (cap or unpriced model): the passes above are incomplete")
    return lines


def write_bench_report(kit: DryRunKit, bench: str, extra: Dict[str, Any]) -> Path:
    """The kit's report plus the bench's own part, as `scripts/memory/s3_dry_run_<bench>_<stamp>.json`."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = inside_memory_dir(kit_module.MEMORY_DIR / f"s3_dry_run_{bench}_{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**kit.report(), "bench": {"name": bench, **extra}}, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return path
