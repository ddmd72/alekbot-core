"""
External Task Agent
===================

The task specialist behind `work_with_external_tasks` (EXTERNAL_TASK_QUEUE_RFC §5.3): the owner's external task queue,
reached by the orchestrator (Smart) with a natural-language instruction (the worker's name and the whole brief, or which
task to read or cancel). It runs its own DelegationEngine loop over the queue's five local tools
(`infrastructure/external_task_tools.py`) and delegates nothing.

The gate, in code and before any model call: a run with a task entry (the owner's own chat turn, or one of the owner's
reminders: `task_entry`), reached straight from a top-level orchestrator (the
call chain is this agent alone: a Smart reached by delegation, and Lelik's `/voice/delegate`, which may name any
intent, are refused), a task queue in this deployment, and a service that does not refuse the owner (the flag and the
account lock, read fresh). A refusal is a success carrying one code-written line per reason (`ANSWER_UNAVAILABLE_*`).

A task it creates in a chat turn waits for the owner's confirmation (the service's `TaskEntry.CHAT`): the owner is shown
its exact text and note and a command only they see, and nothing leaves until they send it. One it creates while a
reminder runs (`TaskEntry.REMINDER`) is queued at once: the owner approved that reminder when it was set, and is shown
the task as a show-only notice (no command) that the create posts itself through `notifications`, the moment the task
is queued, so the notice stands however the reminder's run then ends. Every task carries a note the model writes
from the instruction: the bot's own return instructions, handed back by the completion alert and never shown to a worker.
The entry is computed once per run from the context, never by the model. A worker's result or failure reason read by
`get_external_task` reaches the orchestrator inside its outside-content wrapper.

The answer is composed by code from the pass-through record of every tool call (`external_task_tools.recorded`): every
result verbatim and in order, then the model's short note (plain specialist text, like every specialist's own words),
then a line when the run was cut. A chat create's receipt comes from the record too, so a run cut by the soft
deadline, the turn cap or an engine exception after a create still posts the owner's receipt; the agent answers success whenever it got
that far (a failure would drop the delivery items). The only failure paths: an empty instruction and a prompt that
cannot be built, both before any tool can run.

Nothing of the instruction, the brief or a result is logged: one start line and one summary line of tool names.

No fallback prompt: the Firestore prompt (profile `external_tasks`) is the single source of truth.
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, cast

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse, DeliveryItem
from ..domain.external_task import TaskOrigin
from ..domain.external_task_entry import TaskEntry, task_entry
from ..domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY
from ..domain.external_task_refusal import ExternalTaskRefused
from ..domain.external_task_texts import (
    ANSWER_CUT,
    ANSWER_CUT_BEFORE_ANY_TOOL,
    ANSWER_EMPTY,
    ANSWER_NOTE_HEAD,
    ANSWER_RESULTS_HEAD,
    ANSWER_UNAVAILABLE_OWNER,
    ANSWER_UNAVAILABLE_SYSTEM,
    ANSWER_UNAVAILABLE_TURN,
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_EXTERNAL_TASK_TOOL,
    ERROR_FAILED,
    GET_EXTERNAL_TASK_TOOL,
    LIST_EXTERNAL_TASKS_TOOL,
    LIST_EXTERNAL_WORKERS_TOOL,
    render_queue_state,
)
from ..domain.external_worker import ExternalWorker
from ..domain.llm import PROMPT_CACHE_BOUNDARY, LLMRequest, LLMResponse, Message, MessagePart, ToolCall
from ..domain.retry_policy import NO_RETRY_POLICY
from ..domain.run_deadline import CURRENT_RUN_DEADLINE
from ..domain.turn_clock import CURRENT_TURN_CLOCK
from ..infrastructure.agent_config import EXTERNAL_TASKS as CONFIG
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.agent_manifest import EXTERNAL_TASKS
from ..infrastructure.delegation_engine import DelegationEngine, DelegationResult, LocalToolHandler, ToolResult
from ..infrastructure.external_task_tools import (
    ReceiptPoster,
    build_cancel_external_task_declaration,
    build_create_external_task_declaration,
    build_get_external_task_declaration,
    build_list_external_tasks_declaration,
    build_list_external_workers_declaration,
    make_cancel_handler,
    make_create_handler,
    make_get_handler,
    make_list_handler,
    make_list_workers_handler,
    recorded,
)
from ..ports.llm_port import AgentExecutionContext
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.prompt_builder_port import PromptBuilderPort
    from ..services.external_task_service import ExternalTaskService
    from ..services.user_notification_service import UserNotificationService

AGENT_TYPE = EXTERNAL_TASKS.agent_type
EMPTY_INSTRUCTION = "The instruction is empty: name the worker and give the whole brief, or say which task is meant."

Record = List[Tuple[ToolCall, ToolResult]]


def soft_deadline(started: float, now: float) -> float:
    """`soft_deadline_s` from the start, earlier under the caller's turn clock or its own timeout (monotonic)."""
    deadline = started + CONFIG.soft_deadline_s
    clock = CURRENT_TURN_CLOCK.get()
    if clock is not None:
        deadline = min(deadline, now + clock.call_timeout(now) - CONFIG.caller_margin_s)
    outer = CURRENT_RUN_DEADLINE.get()
    if outer is not None:
        deadline = min(deadline, outer - CONFIG.caller_margin_s)
    return deadline


def refusal_reason(context: Mapping[str, Any], has_service: bool) -> Optional[str]:
    """Why this run may not touch the queue (a short log word), or None: a run with a task entry (the owner's own chat
    turn or a reminder's run), the call chain is this agent alone, a task queue in this deployment and a user. The
    service's own checks come after."""
    if task_entry(context) is None:
        return "no_entry"
    if list(context.get(AgentCoordinator.CALL_CHAIN_KEY) or []) != [EXTERNAL_TASKS.agent_id]:
        return "not_top_level"
    if not has_service:
        return "no_service"
    if not context.get("user_id"):
        return "no_user"
    return None


def task_origin(context: Mapping[str, Any]) -> TaskOrigin:
    """Where a created task's notice goes: the chat the orchestrator's turn came from."""
    return TaskOrigin(platform=str(context.get("origin_platform") or ""),
                      channel_id=str(context.get("origin_channel_id") or ""),
                      session_id=str(context.get("session_id") or ""))


def compose_answer(record: Record, note: str, *, cut: bool) -> str:
    """Every result verbatim and in order, the note, and the cut line."""
    blocks: List[str] = []
    if record:
        blocks.append("\n\n".join([ANSWER_RESULTS_HEAD, *(result.result_str for _, result in record)]))
    if note:
        blocks.append(f"{ANSWER_NOTE_HEAD}\n{note}")
    if cut:
        blocks.append(ANSWER_CUT if record else ANSWER_CUT_BEFORE_ANY_TOOL)
    return "\n\n".join(blocks) or ANSWER_EMPTY


class ExternalTaskAgent(BaseAgent):
    """The task specialist. One instance per user; every piece of a run's state is a local of `execute`."""

    # A whole-execution retry after a create would create the task twice. The same-provider retry inside _call_llm stays.
    RETRY_POLICY = NO_RETRY_POLICY

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        service: Optional["ExternalTaskService"],
        prompt_builder: "PromptBuilderPort",
        thinking: Optional[str] = None,
        monotonic: Callable[[], float] = time.monotonic,
        notifications: Optional["UserNotificationService"] = None,
    ) -> None:
        """`service` is None in a deployment without a task queue: every run then answers `ANSWER_UNAVAILABLE_SYSTEM`.
        `thinking` is the user's `agent_thinking["external_tasks"]`; None takes the config's effort. `notifications`
        posts a reminder create's receipt to the run's channel; None posts none (logged at error level)."""
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._service = service
        self._prompt_builder = prompt_builder
        self.thinking = thinking if thinking is not None else CONFIG.thinking_effort
        self._monotonic = monotonic
        self._notifications = notifications

    async def can_handle(self, message: AgentMessage) -> bool:
        return message.intent == AgentIntent.QUERY

    def _on_agent_start(self, text: str = "") -> None:
        """The instruction carries the owner's brief: never logged."""
        logger.info(f"[{self.agent_id}] start")

    async def execute(self, message: AgentMessage) -> AgentResponse:
        started = self._monotonic()
        context = message.context
        user_id = str(context.get("user_id") or "")
        commission = str(message.payload.get("query") or "")
        self._on_agent_start()
        service = self._service
        reason = refusal_reason(context, service is not None)
        entry = task_entry(context)
        if reason is not None or service is None or entry is None:
            return self._unavailable(message, reason or "no_service")
        if not commission.strip():
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id, error=EMPTY_INSTRUCTION)

        workers_result, prompt_result = await asyncio.gather(
            service.list_workers(user_id), self._build_prompt(user_id, context.get("account_id")),
            return_exceptions=True)
        for result in (workers_result, prompt_result):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result     # a cancel of this turn, never an answer
        if isinstance(workers_result, ExternalTaskRefused):
            return self._unavailable(message, workers_result.reason.value)
        if isinstance(workers_result, Exception):
            logger.error("[ExternalTaskAgent] user=%s worker list failed: %s", user_id[:8],
                         type(workers_result).__name__)
            return self._reply(message, ERROR_FAILED, [])
        if isinstance(prompt_result, Exception):
            self._on_agent_error(prompt_result, "prompt_builder")
            return AgentResponse.failure(task_id=message.task_id, agent_id=self.agent_id,
                                         error=f"PromptBuilder failed: {prompt_result}")
        workers = cast(List[ExternalWorker], workers_result)     # every exception was answered above
        prompt = cast(str, prompt_result)

        instruction = Message(role="user", parts=[MessagePart(text=commission)])
        record: Record = []
        origin = task_origin(context)
        handlers = self._handlers(service, user_id, origin, commission, workers, entry,
                                  self._receipt_poster(user_id, str(context.get("account_id") or ""), origin))
        declarations = self._declarations(workers, entry)
        boundary = "" if PROMPT_CACHE_BOUNDARY in prompt else f"\n\n{PROMPT_CACHE_BOUNDARY}"
        state = render_queue_state([w.label for w in workers])
        base = LLMRequest(model_name=self.model_name, system_instruction=f"{prompt}{boundary}\n\n{state}",
                          messages=[instruction], tools=declarations, thinking=self.thinking,
                          max_tokens=CONFIG.max_tokens)
        deadline = soft_deadline(started, self._monotonic())

        async def call_llm(request: LLMRequest, turn: int = 0) -> LLMResponse:
            left = int(deadline - self._monotonic())
            return await self._call_llm(
                request.model_copy(update={"timeout": max(1, min(CONFIG.call_timeout_s, left))}), turn)

        outcome, cut = await self._run(DelegationEngine(self.coordinator).execute(
            call_llm=call_llm, base_request=base, context=dict(context), max_turns=CONFIG.max_turns,
            calling_agent_id=self.agent_id, local_tools=recorded(handlers, record), use_turn_clock=False,
            allow_delegation=False), deadline, user_id)
        note = (outcome.text or "").strip() if outcome is not None else ""
        answer = compose_answer(record, note, cut=cut)
        self._log_done(user_id, record, cut, started)
        return self._reply(message, answer, record)

    async def _build_prompt(self, user_id: str, account_id: Optional[str]) -> str:
        # No memory facts, directives or location next to an outbound channel.
        return await self._prompt_builder.build_for_agent(
            agent_type=AGENT_TYPE, user_id=user_id, account_id=account_id, include_biographical=False,
            include_directives=False, include_location=False)

    def _receipt_poster(self, user_id: str, account_id: str, origin: TaskOrigin) -> Optional[ReceiptPoster]:
        """Posts a reminder create's receipt to the channel the run speaks in (the task's origin)."""
        notifications = self._notifications
        if notifications is None:
            return None

        async def post(data: Mapping[str, Any]) -> bool:
            return await notifications.post_task_receipt(
                user_id, account_id, data, channel_id_override=origin.channel_id or None,
                platform_override=origin.platform or None)
        return post

    @staticmethod
    def _handlers(service: "ExternalTaskService", user_id: str, origin: TaskOrigin, commission: str,
                  workers: Sequence[ExternalWorker], entry: TaskEntry,
                  post_receipt: Optional[ReceiptPoster]) -> Dict[str, LocalToolHandler]:
        handlers: Dict[str, LocalToolHandler] = {
            LIST_EXTERNAL_TASKS_TOOL: make_list_handler(service, user_id),
            LIST_EXTERNAL_WORKERS_TOOL: make_list_workers_handler(service, user_id),
            GET_EXTERNAL_TASK_TOOL: make_get_handler(service, user_id),
            CANCEL_EXTERNAL_TASK_TOOL: make_cancel_handler(service, user_id),
        }
        if workers:
            handlers[CREATE_EXTERNAL_TASK_TOOL] = make_create_handler(
                service, user_id, origin, commission=commission, entry=entry, post_receipt=post_receipt)
        return handlers

    @staticmethod
    def _declarations(workers: Sequence[ExternalWorker], entry: TaskEntry) -> List[Dict[str, Any]]:
        """With no worker, `create_external_task` is left out: an empty enum is not a valid schema."""
        create = [build_create_external_task_declaration(workers, entry=entry)] if workers else []
        return [*create, build_list_external_tasks_declaration(), build_get_external_task_declaration(),
                build_cancel_external_task_declaration(), build_list_external_workers_declaration()]

    async def _run(self, engine_run: Any, deadline: float, user_id: str) -> Tuple[Optional[DelegationResult], bool]:
        """The engine under the soft deadline. Returns its result (None when it did not finish) and whether the run was
        cut: the deadline, an exception, or the turn cap."""
        engine = asyncio.ensure_future(engine_run)
        outcome: Optional[DelegationResult] = None
        cut = False
        try:
            done, _ = await asyncio.wait({engine}, timeout=max(0.0, deadline - self._monotonic()))
            if engine in done:
                outcome = engine.result()
                cut = outcome.failed
            else:
                cut = True
                logger.warning("[ExternalTaskAgent] user=%s soft deadline reached; answering from what ran",
                               user_id[:8])
        except Exception as exc:   # the answer is built from the record on any exit
            logger.warning("[ExternalTaskAgent] user=%s engine ended with %s", user_id[:8], type(exc).__name__)
            cut = True
        finally:
            if not engine.done():
                engine.cancel()
            await asyncio.gather(engine, return_exceptions=True)
        return outcome, cut

    def _log_done(self, user_id: str, record: Record, cut: bool, started: float) -> None:
        """One summary line: tool names and counts only (never an instruction, a brief or a result)."""
        logger.info("[ExternalTaskAgent] user=%s tools=%s pending=%s cut=%s ms=%s", user_id[:8],
                    ",".join(call.name for call, _ in record) or "-", len(_receipts(record)), cut,
                    int((self._monotonic() - started) * 1000))

    def _unavailable(self, message: AgentMessage, reason: str) -> AgentResponse:
        logger.info("[ExternalTaskAgent] user=%s refused: %s", str(message.context.get("user_id") or "")[:8], reason)
        line = {"no_service": ANSWER_UNAVAILABLE_SYSTEM, "no_entry": ANSWER_UNAVAILABLE_TURN,
                "not_top_level": ANSWER_UNAVAILABLE_TURN, "no_user": ANSWER_UNAVAILABLE_TURN}.get(
            reason, ANSWER_UNAVAILABLE_OWNER)     # the service's refusals: the flag is off, the account is locked
        return self._reply(message, line, [])

    def _reply(self, message: AgentMessage, text: str, record: Record) -> AgentResponse:
        return AgentResponse.success(
            task_id=message.task_id, agent_id=self.agent_id, result=text, delivery_items=_receipts(record),
            history_context=None,
        )


def _receipts(record: Record) -> List[DeliveryItem]:
    """The receipts of every chat create that ran, in call order: the owner's literal record of what waits for their
    confirmation (a reminder create posted its own and returned none)."""
    return [item for _, result in record for item in result.delivery_items
            if item.type == EXTERNAL_TASK_RECEIPT_DELIVERY]
