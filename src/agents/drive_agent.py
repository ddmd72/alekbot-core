"""
Drive Agent
===========

The drive specialist behind `work_with_drive_files` (docs/10_rfcs/DRIVE_SPECIALIST_RFC.md).

The orchestrator commissions it in natural language (plus `file_contents`, the texts to write); it runs its own
DelegationEngine loop over local CRUD tools only (no delegate_to_specialist), each a thin wrapper over the user's shared
DriveOperations, under a soft deadline. Whatever happens — a report, a plain reply, the deadline, a still-running
mutation, an engine exception, the turn limit — it answers with AgentResponse.success carrying the answer code builds
from the pass-through collector (§7): status, "What to do", op lines, reading lines, the note, then every read
verbatim; `metadata.file_data` holds the vision files, `history_context.drive_context` today's refs. So the engine's
"Correct your input and try again" suffix never reaches a drive answer. The only failures: a missing prompt (before any
LLM call), a circuit-breaker refusal, an external cancellation, a hard timeout the soft deadline failed to prevent.

A run that ends at the turn cap without a report (the Sonnet 5.5 filler-tool stall, `decisions/sonnet55_adaptive_thinking_stall.md`:
"no workaround, loud monitoring") is logged at ERROR with the per-turn tool-name sequence and alerted once through
`alert_sink` (consolidation's precedent). The sol fallback does not cover it: it only answers provider errors on the first
call. Neither the log nor the alert carries arguments, file names or more than the first 8 characters of the user id.

No fallback prompt: the Firestore prompt (profile `drive`) is the single source of truth.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.drive_answer import DriveAnswer, compose_answer
from ..domain.drive_file_content_item import FileContentItem
from ..domain.drive_file_contents import parse_file_contents
from ..domain.drive_line import DriveLine
from ..domain.drive_pass_through import PassThroughCollector
from ..domain.drive_preflight import DrivePreflight
from ..domain.drive_report import DriveReport
from ..domain.drive_tool_texts import REPORT_TOOL, drive_tool_declarations
from ..domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, LLMResponse, Message, MessagePart
from ..domain.retry_policy import NO_RETRY_POLICY
from ..domain.run_deadline import CURRENT_RUN_DEADLINE
from ..domain.turn_clock import CURRENT_TURN_CLOCK
from ..infrastructure.agent_config import DRIVE
from ..infrastructure.delegation_engine import DelegationEngine, DelegationResult
from ..infrastructure.drive_tools import DriveToolRun
from ..ports.alert_sink import AlertSinkPort
from ..ports.llm_port import AgentExecutionContext
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..infrastructure.drive_operations import DriveOperations
    from ..ports.prompt_builder_port import PromptBuilderPort

AGENT_TYPE = "drive"
ALERT_TIMEOUT_S = 5.0   # the turn-cap alert never holds the answer back: a hanging sink must not turn it into a timeout (X9)
# Payload keys the commission may carry besides `query` and `file_contents` (accept liberally, §4.2): shown to the
# model as part of the commission, never rejected.
# The Smart-era drive fields are here too: a stale skill, reminder or history block can still tell Smart to set them (X12).
_STRAY_FIELDS = ("file_ref", "folder", "drive_query", "drive_folder", "drive_kind", "drive_date_from", "drive_date_to",
                 "drive_deleted", "new_name", "source_ref", "original", "name")
_STRAY_HEADER = "Also given with the commission:"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def soft_deadline(started: float, now: float) -> float:
    """§5.6: 150 s from the start, earlier under the caller's turn clock or its own timeout (monotonic)."""
    deadline = started + DRIVE.soft_deadline_s
    clock = CURRENT_TURN_CLOCK.get()
    if clock is not None:
        deadline = min(deadline, now + clock.call_timeout(now) - DRIVE.turn_clock_margin_s)
    outer = CURRENT_RUN_DEADLINE.get()
    if outer is not None:
        deadline = min(deadline, outer - DRIVE.run_deadline_margin_s)
    return deadline


def origin_label(context: Mapping[str, Any]) -> str:
    """Whose words `user_message` holds (§5.3)."""
    if context.get("interactive_delivery"):
        return "chat"
    origin = str(context.get("run_origin") or "")
    if origin.startswith("voice:"):
        return "call (Lelik's words)"
    if origin.startswith("background:"):
        return "background: " + origin[len("background:"):]
    return "fallback"


def render_state(context: Mapping[str, Any], items: Sequence[FileContentItem], preflight: DrivePreflight,
                 now: datetime, timezone_name: str) -> str:
    """The state block, after the cache boundary (§5.3)."""
    try:
        local = now.astimezone(ZoneInfo(timezone_name or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        local, timezone_name = now.astimezone(timezone.utc), "UTC"
    parts = list(context.get("current_message_parts") or [])
    words = " ".join(_text(p) for p in parts if _text(p) and not _file_data(p))
    attached = " ".join(_text(p) for p in parts if _file_data(p) and _text(p))
    lines = [
        "drive_state {",
        f"  drive: {preflight.drive.value}",
        f"  search: {preflight.search.value}",
        f"  now: {local:%Y-%m-%d %H:%M} {timezone_name or 'UTC'}",
        f'  user_message ({origin_label(context)}): "{words}"',
        f"  attached_now: {attached or '(none)'}",
        "  file_contents: " + ("; ".join(item.describe() for item in items) if items else "(none)"),
        "}",
    ]
    return "\n".join(lines)


def _text(part: Any) -> str:
    value = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
    return value if isinstance(value, str) else ""


def _file_data(part: Any) -> Any:
    return part.get("file_data") if isinstance(part, dict) else getattr(part, "file_data", None)


class DriveAgent(BaseAgent):
    """The drive specialist (§5). One instance per user, shared DriveOperations; per-execution state only."""

    # A whole-execution retry after a write would repeat the write (the AlekGatewayAgent precedent). The
    # same-provider retry inside _call_llm stays.
    RETRY_POLICY = NO_RETRY_POLICY

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        operations: "DriveOperations",
        prompt_builder: Optional["PromptBuilderPort"] = None,
        user_timezone: str = "UTC",
        thinking: Optional[str] = None,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        alert_sink: Optional[AlertSinkPort] = None,
    ) -> None:
        """`thinking` is the user's `agent_thinking["drive"]`; None takes DRIVE.thinking_effort. `alert_sink` takes the
        one ops alert of a turn-cap exit without a report (None = log only)."""
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._ops = operations
        self._prompt_builder = prompt_builder
        self._user_timezone = user_timezone or "UTC"
        self.thinking = thinking if thinking is not None else DRIVE.thinking_effort
        self._clock = clock
        self._monotonic = monotonic
        self._alert_sink = alert_sink

    @property
    def operations(self) -> "DriveOperations":
        """The shared per-user operations (read-only), mirroring `FileManagementAgent.drive_operations`."""
        return self._ops

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    def _on_agent_start(self, text: str = "") -> None:
        """The commission is the user's words: never logged (§5.1)."""
        logger.info(f"[{self.agent_id}] start")

    async def execute(self, message: AgentMessage) -> AgentResponse:
        started = self._monotonic()
        context = message.context
        user_id = str(context.get("user_id") or "")
        self._on_agent_start()
        items = parse_file_contents(message.payload)
        collector = PassThroughCollector()

        async def no_builder() -> None:
            return None
        builder = self._prompt_builder
        # The preflight reads and the prompt are independent: one round trip, not two (M13).
        preflight_result, prompt_result = await asyncio.gather(
            self._ops.preflight(user_id),
            builder.build_for_agent(
                agent_type=AGENT_TYPE, user_id=user_id, account_id=context.get("account_id"),
                include_biographical=False, include_directives=False) if builder is not None else no_builder(),
            return_exceptions=True)
        if isinstance(preflight_result, BaseException):
            raise preflight_result
        preflight = preflight_result
        if preflight.drive is DriveLine.NOT_CONFIGURED:   # answered at once, no LLM call (§7.2)
            return self._answer(message, compose_answer(collector, DriveReport(), drive_line=preflight.drive),
                                collector, started, turns=0, delivery_items=[])
        if isinstance(prompt_result, BaseException):
            if not isinstance(prompt_result, Exception):
                raise prompt_result
            self._on_agent_error(prompt_result, "prompt_builder")
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error=f"PromptBuilder failed: {prompt_result}")
        if prompt_result is None:
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error="DriveAgent requires a prompt builder")
        prompt = prompt_result

        deadline = soft_deadline(started, self._monotonic())
        still_running = asyncio.Event()
        run = DriveToolRun(self._ops, collector, items, context, user_id, deadline=deadline,
                           on_still_running=still_running.set, clock=self._monotonic)
        state = render_state(context, items, preflight, self._clock(), self._user_timezone)
        boundary = "" if PROMPT_CACHE_BOUNDARY in prompt else f"\n\n{PROMPT_CACHE_BOUNDARY}"
        base = LLMRequest(
            model_name=self.model_name, system_instruction=f"{prompt}{boundary}\n\n{state}",
            messages=[Message(role="user", parts=[MessagePart(text=self._commission(message.payload))])],
            tools=drive_tool_declarations(), thinking=self.thinking, max_tokens=DRIVE.max_tokens,
        )

        tools_per_turn: List[List[str]] = []   # tool names only, for the turn-cap log and alert

        async def call_llm(request: LLMRequest, turn: int = 0) -> LLMResponse:
            if still_running.is_set():
                # A still-running mutation ended the commission (§5.4): no further turn; this run is cancelled next.
                await asyncio.Event().wait()
            run.step = turn
            left = int(deadline - self._monotonic())
            response = await self._call_llm(
                request.model_copy(update={"timeout": max(1, min(DRIVE.call_timeout_s, left))}), turn)
            tools_per_turn.append([tc.name for tc in response.tool_calls])
            return response

        engine = asyncio.ensure_future(DelegationEngine(self.coordinator).execute(
            call_llm=call_llm, base_request=base, context=dict(context), max_turns=DRIVE.max_turns,
            terminal_tool=REPORT_TOOL, calling_agent_id=self.agent_id, local_tools=run.handlers,
            use_turn_clock=False, allow_delegation=False))
        stop = asyncio.ensure_future(still_running.wait())
        outcome: Optional[DelegationResult] = None
        cut = False
        turn_cap = False
        try:
            done, _ = await asyncio.wait({engine, stop}, timeout=max(0.0, deadline - self._monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if engine in done:
                outcome = engine.result()
                cut = outcome.failed and outcome.terminal_tool_args is None
                turn_cap = cut and len(tools_per_turn) >= DRIVE.max_turns
            elif not done:
                cut = True
                logger.warning("[DriveAgent] user=%s soft deadline reached; answering from what ran", user_id[:8])
        except Exception as exc:   # the answer is built from the collector on any exit (§5.4)
            logger.warning("[DriveAgent] engine ended with %s: %s", type(exc).__name__, exc)
            cut = True
        finally:
            for task in (engine, stop):
                if not task.done():
                    task.cancel()
            # Let a cancelled engine's handlers record their calls as not finished before the answer is built.
            await asyncio.gather(engine, stop, return_exceptions=True)
        if turn_cap:
            await self._report_turn_cap(user_id, tools_per_turn)
        report = DriveReport.of(outcome.terminal_tool_args if outcome else None, outcome.text if outcome else "")
        answer = compose_answer(collector, report, cut=cut, drive_line=preflight.drive,
                                search_line=preflight.search)
        return self._answer(message, answer, collector, started, turns=run.step,
                            delivery_items=list(outcome.delivery_items) if outcome else [])

    async def _report_turn_cap(self, user_id: str, tools_per_turn: Sequence[Sequence[str]]) -> None:
        """The turn limit ended the run without a report: ERROR log, and one best-effort ops alert. Tool names only."""
        sequence = " | ".join(f"{n}: {','.join(names) or '(no tool)'}" for n, names in enumerate(tools_per_turn, 1))
        logger.error("[DriveAgent] user=%s hit the turn limit (%s) without a report; tools per turn: %s",
                     user_id[:8], DRIVE.max_turns, sequence)
        if self._alert_sink is None:
            return
        try:
            await asyncio.wait_for(self._alert_sink.post(
                f"🚨 Drive specialist hit the turn limit without a report | model {self.model_name} | "
                f"turns {len(tools_per_turn)}/{DRIVE.max_turns} | user {user_id[:8]}\n{sequence}"),
                timeout=ALERT_TIMEOUT_S)
        except Exception as exc:
            logger.warning("[DriveAgent] ops alert delivery failed: %s", exc)

    @staticmethod
    def _commission(payload: Mapping[str, Any]) -> str:
        """The commission as the model reads it; stray fields are part of it, never rejected (§4.2)."""
        text = str(payload.get("query") or "")
        stray = [f"{name}: {payload[name]}" for name in _STRAY_FIELDS
                 if isinstance(payload.get(name), (str, bool)) and payload[name] not in ("", False)]
        return "\n\n".join([text, _STRAY_HEADER + "\n" + "\n".join(stray)]) if stray else text

    def _answer(self, message: AgentMessage, answer: DriveAnswer, collector: PassThroughCollector, started: float, *,
                turns: int, delivery_items: List[DeliveryItem]) -> AgentResponse:
        """One log line per commission, no file names (§5.1), and the success that carries the answer (§7)."""
        ops = ",".join(f"{r.outcome.op.value}:{r.outcome.cls.value}" for r in collector.results)
        logger.info("[DriveAgent] user=%s status=%s model=%s ops=%s turns=%s ms=%s",
                    str(message.context.get("user_id"))[:8], answer.status.value, self.model_name, ops, turns,
                    int((self._monotonic() - started) * 1000))
        metadata: Dict[str, Any] = {"drive_status": answer.status.value}
        if answer.files:
            metadata["file_data"] = answer.files
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=answer.text, metadata=metadata,
            history_context={"drive_context": answer.drive_context} if answer.drive_context else None,
            delivery_items=delivery_items,
        )
