#!/usr/bin/env python3
"""
Recorded single calls for the system-turn fingerprint work (docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md; plan §2 R1/R2/R5).
OWNER-RUN, PAID. Each case is ONE real LLM call on the owner's account; the turns before it are scripted (free), the code
that answers them is the real production code. The meter prints cost with the three token legs apart.

    NO_PROXY='*' python -m scripts.validation.fingerprint_probe --list
    NO_PROXY='*' python -m scripts.validation.fingerprint_probe --case R2 --case R1.3 --user "$DEV_USER_ID" --max-usd 1.0
    ... [--rule-token PATH] [--live-read] [--max-llm-calls N]

- `--max-usd` is required: the meter (drive_eval/cost_meter.py) refuses a call once the spend has reached it, and no case
  starts once spend + the dearest case so far would pass it. `--max-llm-calls` (default 1) caps the REAL calls per case.
- `--rule on|off` of the plan is `--rule-token`: absent = rule off (what Firestore serves). A token FILE (`<TOKEN_ID>.groovy`)
  is served in place of that token; a snapshot-shaped DIRECTORY (`tokens/{system,user}/*.groovy`, `blueprints/*.yaml`,
  `profiles/*.yaml`) serves every file it holds, through the consolidation kit's read-only overlays.
- `--live-read` lets the delegations a case marks live (R1.8: Gmail open of a message id that does not exist) run for
  real, read-only. Without it they are stubbed with the production note.
- Isolation: sessions are empty (a channel id that holds no history) and nothing is saved to them; every Firestore write is
  blocked and recorded (`install_write_guard`; drive_routing_probe has no such guard); the skill, workspace and memory-draft
  stores are the dry-run wrappers of the other evals; delegations are stubbed (the default answer says nothing ran); the
  notification channel records and never sends. What still happens: Firestore/GCS READS of the owner's data, and the
  `prompt_content` row every real LLM call writes (that row is the record).
- Output: `scripts/memory/fingerprint/<date>/<case>.json` (request text, response, tool calls, cost legs, stubs) and
  `summary.txt`, gitignored. The request holds the owner's memory: it never leaves the machine.

Unverified without a paid call: that the provider accepts the scripted first turn's tool call id (`probe_call_1`) in the
transcript. If a provider refuses it, the case reports the error and costs nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.validation.drive_eval.cost_meter import CostCapReached, Meter, format_call, stop_reason  # noqa: E402
from scripts.validation.fingerprint_cases import ALL_CASES, Case, select  # noqa: E402
from src.agents.base_agent import BaseAgent  # noqa: E402
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse  # noqa: E402
from src.domain.llm import LLMRequest, LLMResponse, MessagePart, ToolCall  # noqa: E402
from src.infrastructure.agent_coordinator import AgentCoordinator  # noqa: E402
from src.infrastructure.notification_sla import NOTIFICATION_SLA  # noqa: E402
from src.services.user_notification_service import UserNotificationService  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = PROJECT_ROOT / "scripts" / "memory" / "fingerprint"
PROBE_CHANNEL = "fingerprint-probe"      # holds no history: the session id built on it is empty
DEFAULT_STUB = "(probe stub) done; nothing ran."
_REAL_HANDLE_DELEGATION = AgentCoordinator.handle_delegation


# ---------------------------------------------------------------------------------------------------------- helpers
def render_request(request: LLMRequest) -> str:
    """The request as text: model, tool names, system instruction, then every message part in order."""
    names: List[str] = []
    for tool in request.tools or []:
        decls = tool.get("function_declarations") if isinstance(tool, dict) else None
        for d in (decls or [tool]):
            names.append(str(d.get("name") if isinstance(d, dict) else getattr(d, "name", d)))
    lines = [f"[model] {request.model_name}", f"[tools] {', '.join(names)}", "[system]", request.system_instruction or ""]
    for message in request.messages:
        for part in message.parts:
            if part.text:
                lines.append(f"[{message.role}] {part.text}")
            if part.tool_call:
                lines.append(f"[{message.role} tool_call] {part.tool_call.name} "
                             f"{json.dumps(part.tool_call.args, ensure_ascii=False)}")
            if part.tool_response:
                lines.append(f"[{message.role} tool_result] {json.dumps(part.tool_response, ensure_ascii=False, default=str)}")
    return "\n".join(lines)


def install_write_guard() -> Tuple[Callable[[], None], List[str]]:
    """No Firestore write of this process reaches the database: every document write and batch commit is recorded and
    answered with nothing. Returns (uninstall, the list the blocked writes are appended to)."""
    from google.cloud.firestore_v1.async_batch import AsyncWriteBatch
    from google.cloud.firestore_v1.async_document import AsyncDocumentReference
    from google.cloud.firestore_v1.async_transaction import AsyncTransaction

    blocked: List[str] = []
    targets = [(AsyncDocumentReference, n) for n in ("set", "update", "delete", "create")] + [
        (AsyncWriteBatch, "commit"), (AsyncTransaction, "_commit"), (AsyncTransaction, "commit")]
    originals: List[Tuple[Any, str, Any]] = []

    def make(owner: Any, method: str) -> Callable[..., Awaitable[Any]]:
        async def blocked_write(self: Any, *a: Any, **kw: Any) -> list:
            blocked.append(f"{owner.__name__}.{method} {getattr(self, 'path', '')}".strip())
            return []
        return blocked_write

    for cls, name in targets:
        if hasattr(cls, name):
            originals.append((cls, name, getattr(cls, name)))
            setattr(cls, name, make(cls, name))

    def uninstall() -> None:
        for cls, name, original in originals:
            setattr(cls, name, original)
    return uninstall, blocked


@dataclass
class Injector:
    """Wraps `BaseAgent._call_llm` (outside the meter): the first call of the agent under test answers a scripted tool
    call (free, unmetered, not recorded); the next `max_real` calls go to the provider and are recorded; any later call
    gets a fixed stop text, so a case never pays for a step it did not ask for."""
    agent_prefix: str
    first_call: Optional[Tuple[str, Dict[str, Any]]]
    max_real: int = 1
    scripted: bool = False
    recorded: List[Dict[str, Any]] = field(default_factory=list)

    def install(self) -> Callable[[], None]:
        original = BaseAgent._call_llm
        me = self

        async def wrapped(agent: BaseAgent, request: LLMRequest, turn: int = 0, **kwargs: Any) -> LLMResponse:
            if not str(agent.config.agent_type).startswith(me.agent_prefix):
                return await original(agent, request, turn, **kwargs)
            if me.first_call and not me.scripted:
                me.scripted = True
                name, args = me.first_call
                return LLMResponse(text=None, tool_calls=[ToolCall(name=name, args=args, thought_signature="probe_call_1")])
            if len(me.recorded) >= me.max_real:
                return LLMResponse(text="(probe stop: real-call cap reached)", tool_calls=[])
            entry: Dict[str, Any] = {"request_text": render_request(request)}
            me.recorded.append(entry)
            response = await original(agent, request, turn, **kwargs)
            entry["response_text"] = response.text or ""
            entry["tool_calls"] = [{"name": tc.name, "args": tc.args} for tc in response.tool_calls]
            return response

        BaseAgent._call_llm = wrapped   # type: ignore[method-assign]

        def uninstall() -> None:
            BaseAgent._call_llm = original   # type: ignore[method-assign]
        return uninstall


class RecordingChannel:
    """A response channel that records every call and sends nothing."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[None]]:
        async def record(*args: Any, **kwargs: Any) -> None:
            self.sent.append({"method": name, "args": [str(a) for a in args],
                              "kwargs": {k: str(v) for k, v in kwargs.items()}})
        return record


class _NotificationState:
    """Notification state with one primary channel; saves are dropped."""

    def __init__(self, user_id: str) -> None:
        from src.domain.notification import NotificationChannel
        self._channel = NotificationChannel(user_id=user_id, platform="slack", channel_id=PROBE_CHANNEL,
                                            updated_at=datetime.now(timezone.utc))

    async def get_primary(self, user_id: str) -> Any:
        return self._channel

    async def get(self, user_id: str) -> Any:
        return self._channel

    async def save(self, *a: Any, **kw: Any) -> None:
        return None

    save_primary = save


class _ChannelFactory:
    def __init__(self, channel: RecordingChannel) -> None:
        self._channel = channel

    def create(self, platform: str, channel_id: str) -> RecordingChannel:
        return self._channel


@dataclass
class Runtime:
    """What a case runs against: the agents by agent-type prefix, the coordinator (route_message / handle_delegation),
    the ids, the meter and the output folder."""
    agents: Mapping[str, Any]
    coordinator: Any
    user_id: str
    account_id: str
    meter: Meter
    out_dir: Path
    live_read: bool = False
    max_llm_calls: int = 1


# ---------------------------------------------------------------------------------------------------------- one case
def _patch_delegation(case: Case, live_read: bool, log: List[Dict[str, Any]]) -> Callable[[], None]:
    """Every delegation is answered here (the default stub says nothing ran); a case's own stub formats the note it
    studies, and its real/live intents reach the real coordinator."""
    real = set(case.real_intents) | (set(case.live_intents) if live_read else set())

    async def handle(self: Any, intent: str, query: str, context: Dict[str, Any],
                     calling_agent_id: str = "unknown") -> AgentResponse:
        if intent in real:
            response = await _REAL_HANDLE_DELEGATION(self, intent, query, context, calling_agent_id)
            log.append({"intent": intent, "query": query, "route": "real", "status": str(response.status)})
            return response
        ok, text = case.stubs[intent](intent, query) if intent in case.stubs else (True, DEFAULT_STUB)
        log.append({"intent": intent, "query": query, "route": "stub", "ok": ok, "text": text})
        if ok:
            return AgentResponse.success(task_id="probe", agent_id="probe_stub", result=text)
        return AgentResponse.failure(task_id="probe", agent_id="probe_stub", error=text)

    AgentCoordinator.handle_delegation = handle   # type: ignore[method-assign]

    def undo() -> None:
        AgentCoordinator.handle_delegation = _REAL_HANDLE_DELEGATION   # type: ignore[method-assign]
    return undo


def _patch_specialist_inputs(case: Case) -> Callable[[], None]:
    """The drive specialist's open, and FileConversionService.resolve_file, answer the production note of the case."""
    undo: List[Callable[[], None]] = []
    if case.drive_open_note is not None:
        from src.domain.drive_op import DriveOp
        from src.domain.drive_outcome import DriveOutcome
        from src.domain.drive_outcome_class import OutcomeClass
        from src.infrastructure.drive_operations import DriveOperations

        original_open = DriveOperations.open
        note = case.drive_open_note()

        async def open_note(self: Any, *a: Any, **kw: Any) -> DriveOutcome:
            return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text=note, success=False)
        DriveOperations.open = open_note   # type: ignore[method-assign]
        undo.append(lambda: setattr(DriveOperations, "open", original_open))
    if case.resolve_file_line is not None:
        from src.domain.exceptions import FileNotResolved
        from src.services.file_conversion_service import FileConversionService

        original_resolve = FileConversionService.resolve_file
        line = case.resolve_file_line()

        async def resolve_refused(self: Any, *a: Any, **kw: Any) -> Any:
            raise FileNotResolved(line)
        FileConversionService.resolve_file = resolve_refused   # type: ignore[method-assign]
        undo.append(lambda: setattr(FileConversionService, "resolve_file", original_resolve))

    def restore() -> None:
        for fn in reversed(undo):
            fn()
    return restore


def _chat_message(case: Case, rt: Runtime, agent: Any) -> AgentMessage:
    ctx: Dict[str, Any] = {"user_id": rt.user_id, "account_id": rt.account_id,
                           "session_id": f"{rt.user_id}:{PROBE_CHANNEL}",
                           "current_message_parts": [MessagePart(text=case.text)]}
    if case.interactive:
        ctx["interactive_delivery"] = True
    return AgentMessage.create(sender="fingerprint_probe", recipient=agent.agent_id, intent=AgentIntent.QUERY,
                               payload={"text": case.text}, context=ctx)


async def _drive_case(case: Case, rt: Runtime) -> Dict[str, Any]:
    ctx = {"user_id": rt.user_id, "account_id": rt.account_id, "session_id": f"{rt.user_id}:{PROBE_CHANNEL}"}
    response = await _REAL_HANDLE_DELEGATION(rt.coordinator, "work_with_drive_files", case.text, ctx, "fingerprint_probe")
    return {"status": str(response.status), "answer": str(response.result if response.result is not None else response.error)}


async def _notify_case(case: Case, rt: Runtime) -> Dict[str, Any]:
    channel = RecordingChannel()
    service = UserNotificationService(
        state_repo=_NotificationState(rt.user_id), channel_factory=_ChannelFactory(channel),   # type: ignore[arg-type]
        coordinator=rt.coordinator, notification_sla=NOTIFICATION_SLA, session_store=None)
    assert case.notify is not None
    result = await service.notify(user_id=rt.user_id, account_id=rt.account_id, **case.notify(rt.user_id))
    return {"delivered": result.delivered, "agent_status": str(result.agent_status), "error": result.error,
            "channel_calls": channel.sent}


async def run_case(case: Case, rt: Runtime) -> Dict[str, Any]:
    """Run one case and write its JSON; returns the result dict (also the summary line's source)."""
    mark = rt.meter.mark()
    injector = Injector(case.agent, case.first_call, max_real=rt.max_llm_calls)
    stub_log: List[Dict[str, Any]] = []
    uninstall_injector = injector.install()            # outside the meter: scripted and stop turns are never metered
    undo_delegation = _patch_delegation(case, rt.live_read, stub_log)
    undo_inputs = _patch_specialist_inputs(case)
    outcome: Dict[str, Any] = {}
    error = ""
    try:
        if case.kind == "chat":
            agent = next(a for prefix, a in rt.agents.items() if case.agent.startswith(prefix))
            response = await agent.execute(_chat_message(case, rt, agent))
            outcome = {"status": str(response.status),
                       "answer": str(response.result if response.result is not None else response.error)}
        elif case.kind == "drive":
            outcome = await _drive_case(case, rt)
        else:
            outcome = await _notify_case(case, rt)
    except CostCapReached as exc:
        error = f"cost cap: {exc}"
    except Exception as exc:   # a case that fails is reported and recorded, never hidden
        error = f"{type(exc).__name__}: {exc}"
    finally:
        undo_inputs()
        undo_delegation()
        uninstall_injector()
    real = injector.recorded[0] if injector.recorded else {}
    result = {
        "case": case.id, "title": case.title, "read_for": case.read_for, "user_text": case.text,
        "scripted_first_call": list(case.first_call) if case.first_call else None,
        "real_calls": len(injector.recorded), "request_text": real.get("request_text", ""),
        "response_text": real.get("response_text", ""), "tool_calls": real.get("tool_calls", []),
        "cost": {"usd": rt.meter.usd_since(mark), "calls": rt.meter.since(mark)}, "delegations": stub_log,
        "outcome": outcome, "error": error, "all_real_calls": injector.recorded,
    }
    rt.out_dir.mkdir(parents=True, exist_ok=True)
    (rt.out_dir / f"{case.id}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def summary_line(result: Dict[str, Any]) -> str:
    cost = "; ".join(format_call(c) for c in result["cost"]["calls"]) or "no metered call"
    steps = [tc["name"] for tc in result["tool_calls"]] or [(result["response_text"] or "")[:80].replace("\n", " ")]
    note = f" ERROR {result['error']}" if result["error"] else ""
    if "SYSTEM ERROR: unknown tool" in result["request_text"]:
        note += " WARNING the scripted tool is not declared for this agent (is the service behind it wired?): case void"
    if not result["real_calls"] and not result["error"]:
        note = " NO REAL CALL HAPPENED (the agent answered without the model)"
    return f"{result['case']}: real_calls={result['real_calls']} {cost} -> {steps}{note}"


async def run_cases(cases: List[Case], rt: Runtime) -> int:
    """Run the cases in order under the cost stop; returns the exit code (1: a case errored or the run was stopped)."""
    case_costs: List[float] = []
    failed = False
    rt.out_dir.mkdir(parents=True, exist_ok=True)
    with (rt.out_dir / "summary.txt").open("a", encoding="utf-8") as summary:
        for case in cases:
            why = stop_reason(rt.meter, case_costs)
            if why:
                line = f"STOPPED before {case.id}: {why}"
                print(line)
                summary.write(line + "\n")
                return 1
            result = await run_case(case, rt)
            case_costs.append(result["cost"]["usd"])
            line = summary_line(result)
            print(line)
            summary.write(line + "\n")
            failed = failed or bool(result["error"])
        total = f"spent ${rt.meter.spent:.4f} of --max-usd {rt.meter.max_usd}"
        print(total)
        summary.write(total + "\n")
    return 1 if failed else 0


# ---------------------------------------------------------------------------------------------------------- rule overlay
def install_rule_overlay(assembly_service: Any, path: Path) -> str:
    """Serve the rule's token (a file) or a snapshot-shaped directory through the kit's read-only overlays."""
    from scripts.consolidation.s3_dry_run_kit import (
        SnapshotBlueprintOverlay, SnapshotProfileOverlay, SnapshotTokenOverlay)

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"--rule-token {path} does not exist")
    root = path
    if path.is_file():   # a lone token: lay it out as a one-token snapshot
        root = Path(tempfile.mkdtemp(prefix="fingerprint_rule_"))
        (root / "tokens" / "system").mkdir(parents=True)
        (root / "tokens" / "system" / f"{path.stem}.groovy").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tokens = sorted({p.stem for sub in ("system", "user") for p in (root / "tokens" / sub).glob("*.groovy")})
    blueprints = sorted(p.stem for p in (root / "blueprints").glob("*.yaml"))
    profiles = sorted(p.stem for p in (root / "profiles").glob("*.yaml"))
    if tokens:
        assembly_service.token_repo = SnapshotTokenOverlay(
            assembly_service.token_repo, assembly_service.security_port, root, tokens)
    if blueprints:
        assembly_service.blueprint_repo = SnapshotBlueprintOverlay(assembly_service.blueprint_repo, root, blueprints)
    if profiles:
        assembly_service.profile_repo = SnapshotProfileOverlay(assembly_service.profile_repo, root, profiles)
    assembly_service.invalidate_cache()
    return f"rule overlay: tokens={tokens} blueprints={blueprints} profiles={profiles}"


# ---------------------------------------------------------------------------------------------------------- live runtime
async def build_runtime(args: argparse.Namespace, out_dir: Path) -> Tuple[Runtime, Callable[[], None], List[str], str]:
    from dotenv import load_dotenv
    load_dotenv()
    from google.cloud import firestore

    from scripts.validation.drive_eval.smart_dry_run import DryRunSkills, RecordingMemoryFacts
    from src.adapters.firestore_account_repo import FirestoreAccountRepository
    from src.adapters.firestore_user_repo import FirestoreUserRepository
    from src.composition.service_container import ServiceContainer
    from src.composition.user_agent_factory import UserAgentFactory
    from src.config.settings import load_settings
    from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
    from src.infrastructure.agent_registry import AgentRegistry
    from src.services.dry_run_workspace import DryRunWorkspace

    user_id = args.user or os.environ["DEV_USER_ID"]
    account_id = args.account or os.environ["DEV_ACCOUNT_ID"]
    uninstall_guard, blocked = install_write_guard()
    db = firestore.AsyncClient(database=os.getenv("FIRESTORE_DATABASE", "us-production"))
    settings = load_settings()
    env_config = settings["ENVIRONMENT_CONFIG"]
    account_repo = FirestoreAccountRepository(db_client=db, collection_name=env_config.account_collection_name)
    user_repo = FirestoreUserRepository(db, env_config, account_repo)
    container = ServiceContainer(config=settings, db_client=db, env_config=env_config, account_repo=account_repo)
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    coordinator = AgentCoordinator(registry=registry)
    factory = UserAgentFactory(config=settings, env_config=env_config, coordinator=coordinator, user_repo=user_repo,
                               account_repo=account_repo, **container.agent_services())
    rule_line = "rule: off (prompts as Firestore serves them)"
    if args.rule_token:
        rule_line = "rule: ON, " + install_rule_overlay(container.assembly_service, Path(args.rule_token))
    cache = await factory.ensure_agents_for_user(user_id)
    agents = {"smart_response": cache["smart_agent"], "quick_response": cache["quick_agent"]}
    for agent in agents.values():   # the dry-run stores of the other evals: nothing is ever saved
        if getattr(agent, "skill_service", None) is not None:
            agent.skill_service = DryRunSkills(agent.skill_service)
        if getattr(agent, "workspace_service", None) is not None:
            agent.workspace_service = DryRunWorkspace(agent.workspace_service)
        agent.memory_fact_service = RecordingMemoryFacts()
    rt = Runtime(agents=agents, coordinator=coordinator, user_id=user_id, account_id=account_id,
                 meter=Meter(max_usd=args.max_usd), out_dir=out_dir, live_read=args.live_read,
                 max_llm_calls=args.max_llm_calls)
    return rt, uninstall_guard, blocked, rule_line


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Recorded single LLM calls for the system-turn fingerprint work (paid)")
    ap.add_argument("--case", action="append", default=[], help="case id (R1, R1.3, R2, R5.1); repeatable")
    ap.add_argument("--list", action="store_true", help="list the case ids and stop; calls nothing")
    ap.add_argument("--user", default=None, help="user id (default: env DEV_USER_ID)")
    ap.add_argument("--account", default=None, help="account id (default: env DEV_ACCOUNT_ID)")
    ap.add_argument("--max-usd", type=float, default=None, help="REQUIRED hard stop on the estimated spend")
    ap.add_argument("--rule-token", default=None,
                    help="a token file or snapshot-shaped directory served as the rule (default: rule off)")
    ap.add_argument("--live-read", action="store_true", help="run the cases' read-only live delegations (Gmail) for real")
    ap.add_argument("--max-llm-calls", type=int, default=1, help="real LLM calls per case (default 1)")
    return ap


async def main(args: argparse.Namespace) -> int:
    cases = select(args.case)
    out_dir = OUT_ROOT / date.today().isoformat()
    rt, uninstall_guard, blocked, rule_line = await build_runtime(args, out_dir)
    uninstall_meter = rt.meter.install()
    try:
        print(rule_line)
        code = await run_cases(cases, rt)
    finally:
        uninstall_meter()
        uninstall_guard()
    if blocked:
        print(f"{len(blocked)} Firestore write(s) were blocked and not made: {sorted(set(blocked))}")
    print(f"output: {out_dir}")
    return code


if __name__ == "__main__":
    parsed = build_parser().parse_args()
    if parsed.list:
        for c in ALL_CASES:
            print(f"{c.id:6} {c.title}")
        sys.exit(0)
    if not parsed.case:
        sys.exit("--case is required (see --list)")
    if parsed.max_usd is None:
        sys.exit("--max-usd is required: a paid run has a hard stop")
    sys.exit(asyncio.run(main(parsed)))
