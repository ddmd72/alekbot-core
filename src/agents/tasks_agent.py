"""
TasksAgent — specialist agent for MS To Do task management.

One intent: manage_user_tasks
  payload: {"query": "<delegation instruction from orchestrator>"}

Tool-calling loop:
  1. Build system prompt with bio context via PromptBuilderPort.
  2. Pass 5 CRUD tool declarations to the LLM.
  3. LLM selects tool(s).
  4. Execute each tool call — CRUD via TasksProviderPort, search via TaskIndexingService.
  5. Repeat until LLM produces a final text response, MAX_TURNS is reached, or the run's time budget
     (the agent's timeout minus _ANSWER_RESERVE_S) is spent. Then one tool-less turn answers from the
     tool results gathered so far; if even that does not fit, the tool results are returned as they are.
     The timeout never cancels a run that already has facts.

list_tasks flow (the lookup tool):
  tasks_provider.list_tasks() → filter by due period → sort (due soonest first, undated newest first)
  → one page of _PAGE_SIZE with total / page / pages and a note saying whether more remain.
  Graph's To Do list call documents no $orderby/$top/$filter on due dates, so sorting, the period
  filter and paging run here, over the list the adapter has already fetched.

search_tasks flow (long lists only):
  task_indexing.search() → List[TaskSearchEntry] → batch_get_tasks() → full Task objects

create_task flow:
  tasks_provider.create_task() → task_indexing.index_task()

update_task / delete_task:
  the LLM passes task_ref (the short `ref` from a prior search_tasks / list_tasks result);
  list_id and task_id are resolved from it by TaskIndexingService.resolve_short_id.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.task import (
    RecurrencePattern,
    RecurrenceRange,
    Task,
    TaskCreate,
    TaskImportance,
    TaskRecurrence,
    TaskStatus,
    TaskUpdate,
)
from ..infrastructure.agent_config import TASKS as TASKS_CFG
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.tasks_provider_port import TasksProviderPort
if TYPE_CHECKING:
    from ..services.task_indexing_service import TaskIndexingService
from ..utils.logger import logger

_MAX_TURNS = 6
# Tasks per list_tasks page: a lookup over a small list is one call and one answer.
_PAGE_SIZE = 20
# Seconds of the run's budget kept for the final, tool-less answer: no tool turn runs past the budget minus this.
_ANSWER_RESERVE_S = 8.0
# The final answer stops this many seconds before the agent's own timeout would cancel the run.
_FINAL_MARGIN_S = 1.0
# The text of the final, tool-less turn (turn cap or time budget reached).
_FORCE_ANSWER = "Answer the delegation now from the tool results above. Call no more tools."
# Characters of tool results kept in the answer when no model turn is left to write one.
_FACTS_MAX_CHARS = 6000

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """An aware UTC datetime for ordering and comparing; a naive value is read as UTC (Graph sends UTC wall times)."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Tool declarations
# ---------------------------------------------------------------------------

_TOOL_DECLARATIONS: List[Dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": (
            "The first tool for every lookup ('is X in my tasks', 'what do I have', 'what is due this week') and for "
            "finding a task to change: returns one page of the user's tasks (20 per page), tasks with a due date "
            "first (soonest first), then tasks without one (newest first). The result says how many tasks there are "
            "and whether more pages remain. When it says these are all the tasks, answer from them: judge yourself "
            "which task the request means, its words need not match. Each task carries its `ref` "
            "(pass it as task_ref to update_task / delete_task)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "integer",
                    "description": (
                        "Page number, from 1 (the default). Ask for the next page only when the result says "
                        "more pages remain and the answer is not on this one."
                    ),
                },
                "due_from": {
                    "type": "string",
                    "description": (
                        "Only tasks due on or after this date/time (ISO-8601, e.g. 2026-11-09). Tasks without a "
                        "due date are left out when due_from or due_to is set. Omit for all tasks."
                    ),
                },
                "due_to": {
                    "type": "string",
                    "description": (
                        "Only tasks due on or before this date/time (ISO-8601; a date alone includes that whole "
                        "day). Omit for all tasks."
                    ),
                },
                "show_completed": {
                    "type": "boolean",
                    "description": "If true, completed tasks are listed too. Default: open tasks only.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_tasks",
        "description": (
            "Semantic search of the user's tasks, for a long task list only: use it when list_tasks reports more "
            "than one page and the delegation names a topic. With one page of tasks, list_tasks already shows "
            "every task: do not search. One search per topic: a miss means no task is close to it, and other "
            "words for the same topic will not find more (page through list_tasks instead). Returns matching "
            "tasks, each with its `ref` (pass it as task_ref to update_task / delete_task)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to search for in tasks.",
                },
                "show_completed": {
                    "type": "boolean",
                    "description": "If true, include completed tasks in search results.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_task",
        "description": "Add a new task to the user's task list.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title — concise imperative phrase, e.g. 'Buy milk'.",
                },
                "body": {
                    "type": "string",
                    "description": "Extra detail or notes. Omit if not provided.",
                },
                "due_datetime": {
                    "type": "string",
                    "description": "Due date/time in ISO-8601. Omit if not mentioned.",
                },
                "start_datetime": {
                    "type": "string",
                    "description": "Start date/time in ISO-8601. Omit if not mentioned.",
                },
                "reminder_datetime": {
                    "type": "string",
                    "description": (
                        "Reminder alert date/time in ISO-8601. "
                        "Set when the user specifies a reminder time. "
                        "When omitted and due_datetime is provided, reminder is automatically "
                        "set to 8pm the day before the due date."
                    ),
                },
                "importance": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "Task importance. Default: normal.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Classification tags. Auto-infer from context — "
                        "e.g. 'remind me about Prague hotel' → ['prague', 'trip']."
                    ),
                },
                "recurrence": {
                    "type": "object",
                    "description": "Recurrence settings. Include only when the task should repeat.",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "enum": ["daily", "weekdays", "weekly", "absoluteMonthly", "absoluteYearly"],
                            "description": (
                                "Repeat frequency: "
                                "'daily' — every N days; "
                                "'weekdays' — Mon–Fri; "
                                "'weekly' — specific days of the week; "
                                "'absoluteMonthly' — same day each month; "
                                "'absoluteYearly' — same day each year."
                            ),
                        },
                        "interval": {
                            "type": "integer",
                            "description": "Every N periods (e.g. 2 = every other week). Default 1.",
                        },
                        "days_of_week": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "For 'weekly': days to repeat, e.g. ['monday', 'friday']. "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                        "day_of_month": {
                            "type": "integer",
                            "description": (
                                "For 'absoluteMonthly'/'absoluteYearly': day of month (1–31). "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                        "month": {
                            "type": "integer",
                            "description": (
                                "For 'absoluteYearly': month (1–12). "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                    },
                    "required": ["pattern"],
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "Modify an existing task — rename, reschedule, set reminder, change recurrence, "
            "mark done, or mark undone. "
            "Requires task_ref from a prior search_tasks or list_tasks result. "
            "Only include fields that should change; omit all others."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_ref": {
                    "type": "string",
                    "description": "Short task reference (the 'ref' field from search_tasks or list_tasks).",
                },
                "title": {
                    "type": "string",
                    "description": "New title. Omit if not changing.",
                },
                "body": {
                    "type": "string",
                    "description": "New notes/body. Omit if not changing.",
                },
                "due_datetime": {
                    "type": "string",
                    "description": (
                        "New due date/time in ISO-8601. Omit if not changing. "
                        "When changed without an explicit reminder_datetime, reminder is "
                        "automatically moved to 8pm the day before the new due date."
                    ),
                },
                "start_datetime": {
                    "type": "string",
                    "description": "New start date/time in ISO-8601. Omit if not changing.",
                },
                "reminder_datetime": {
                    "type": "string",
                    "description": (
                        "New reminder alert date/time in ISO-8601. "
                        "Set when the user explicitly specifies a reminder time. "
                        "Omit to let auto-reminder logic apply when due_datetime is also changing."
                    ),
                },
                "is_reminder_on": {
                    "type": "boolean",
                    "description": "Set false to disable the reminder. Omit if not changing.",
                },
                "importance": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "New importance level. Omit if not changing.",
                },
                "status": {
                    "type": "string",
                    "enum": ["notStarted", "inProgress", "completed", "deferred", "waitingOnOthers"],
                    "description": "New status. Use 'completed' to mark done, 'notStarted' to unmark.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Full replacement tag list. Omit if not changing.",
                },
                "recurrence": {
                    "type": "object",
                    "description": "New recurrence settings. Replaces existing recurrence entirely.",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "enum": ["daily", "weekdays", "weekly", "absoluteMonthly", "absoluteYearly"],
                            "description": (
                                "Repeat frequency: "
                                "'daily' — every N days; "
                                "'weekdays' — Mon–Fri; "
                                "'weekly' — specific days of the week; "
                                "'absoluteMonthly' — same day each month; "
                                "'absoluteYearly' — same day each year."
                            ),
                        },
                        "interval": {
                            "type": "integer",
                            "description": "Every N periods (e.g. 2 = every other week). Default 1.",
                        },
                        "days_of_week": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "For 'weekly': days to repeat, e.g. ['monday', 'friday']. "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                        "day_of_month": {
                            "type": "integer",
                            "description": (
                                "For 'absoluteMonthly'/'absoluteYearly': day of month (1–31). "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                        "month": {
                            "type": "integer",
                            "description": (
                                "For 'absoluteYearly': month (1–12). "
                                "Derived from due_datetime when omitted."
                            ),
                        },
                    },
                    "required": ["pattern"],
                },
            },
            "required": ["task_ref"],
        },
    },
    {
        "name": "delete_task",
        "description": (
            "Permanently remove a task. "
            "Requires task_ref from a prior search_tasks or list_tasks result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_ref": {
                    "type": "string",
                    "description": "Short task reference (the 'ref' field from search_tasks or list_tasks).",
                },
            },
            "required": ["task_ref"],
        },
    },
]


class TasksAgent(BaseAgent):
    """
    Specialist agent for MS To Do task management.

    Receives a natural-language delegation from the orchestrator.
    Uses a tool-calling loop to select and execute the right CRUD operation.
    """

    MAX_TOKENS = TASKS_CFG.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        tasks_provider: TasksProviderPort,
        task_indexing: TaskIndexingService,
        user_id: Optional[str] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._tasks = tasks_provider
        self._indexing = task_indexing
        self.user_id = user_id
        self._monotonic = monotonic

        logger.info(
            f"📋 TasksAgent initialized "
            f"(model={self.model_name}, user={user_id[:8] if user_id else 'NONE'})"
        )

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return bool(message.payload.get("query", ""))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        reasoning = message.payload.get("background", "")
        user_id = message.context.get("user_id") or self.user_id
        account_id = message.context.get("account_id")

        self._on_agent_start(query)
        start_time = time.time()

        system_prompt = ""
        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="tasks",
                user_id=user_id,
                account_id=account_id,
                routing_metadata=None,
                include_biographical=True,
            )
        except Exception as exc:
            logger.warning(
                f"📋 TasksAgent: prompt build failed ({exc}), proceeding without bio context"
            )
        user_text = query
        if reasoning:
            user_text = f"{query}\n\nContext: {reasoning}"

        messages: List[Message] = [
            Message(role="user", parts=[MessagePart(text=user_text)])
        ]

        final_text = ""
        answered = False
        tool_turns = 0
        timeout_ms = message.timeout_ms if message.timeout_ms is not None else self.config.timeout_ms
        end: Optional[float] = None if timeout_ms is None else self._monotonic() + timeout_ms / 1000

        for turn in range(_MAX_TURNS):
            tool_budget = self._seconds_left(end, _ANSWER_RESERVE_S)
            if tool_budget is not None and tool_budget <= 0:
                logger.warning(f"📋 TasksAgent: time budget spent after {tool_turns} tool turn(s), answering now")
                break
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                tools=_TOOL_DECLARATIONS,
                max_tokens=self.MAX_TOKENS,
                disable_safety=True,
            )
            try:
                response = await asyncio.wait_for(self._call_llm(request, turn=turn + 1), timeout=tool_budget)
            except asyncio.TimeoutError:
                logger.warning(f"📋 TasksAgent: turn {turn + 1} ran past the time budget, answering now")
                break

            if not response.tool_calls:
                final_text = response.text or ""
                answered = True
                break

            messages.append(Message(
                role="model",
                raw_content=response.raw_content,
                parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
            ))

            tool_result_parts: List[MessagePart] = []
            for tool_call in response.tool_calls:
                logger.info(
                    f"📋 TasksAgent tool_call: {tool_call.name} "
                    f"args={json.dumps(tool_call.args, ensure_ascii=False)[:200]}"
                )
                try:
                    result = await self._execute_tool(tool_call.name, tool_call.args, user_id or "")
                except Exception as exc:
                    logger.warning(f"📋 TasksAgent: tool '{tool_call.name}' failed: {exc}")
                    result = {"error": str(exc)}

                tool_result_parts.append(
                    MessagePart(tool_response={"name": tool_call.name, "response": result})
                )

            messages.append(Message(role="user", parts=tool_result_parts))
            tool_turns += 1

        else:
            logger.warning(f"📋 TasksAgent: max turns ({_MAX_TURNS}) reached, forcing format")

        if not answered and tool_turns:
            final_text = await self._final_answer(system_prompt, messages, end, turn=tool_turns + 1)

        duration_ms = int((time.time() - start_time) * 1000)

        if not final_text:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error="TasksAgent produced no response",
            )

        self._on_agent_success(
            char_count=len(final_text),
            token_count=0,
            output_text=final_text,
        )
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=final_text,
            confidence=1.0,
            metadata={"duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Time budget and the final answer
    # ------------------------------------------------------------------

    def _seconds_left(self, end: Optional[float], reserve: float) -> Optional[float]:
        """Seconds until `end - reserve`; None when the run has no timeout."""
        return None if end is None else end - reserve - self._monotonic()

    async def _final_answer(
        self, system_prompt: str, messages: List[Message], end: Optional[float], turn: int,
    ) -> str:
        """One tool-less turn that answers from the tool results so far, bounded by the time left; when it cannot
        run, times out or returns nothing, the tool results themselves are the answer (never a failure)."""
        left = self._seconds_left(end, _FINAL_MARGIN_S)
        if left is None or left > 0:
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages + [Message(role="user", parts=[MessagePart(text=_FORCE_ANSWER)])],
                tools=[],
                max_tokens=self.MAX_TOKENS,
                disable_safety=True,
            )
            try:
                response = await asyncio.wait_for(self._call_llm(request, turn=turn), timeout=left)
                if response.text:
                    return response.text
                logger.warning("📋 TasksAgent: the final answer turn returned no text, returning the tool results")
            except asyncio.TimeoutError:
                logger.warning("📋 TasksAgent: the final answer turn ran out of time, returning the tool results")
        return self._facts_answer(messages)

    @staticmethod
    def _facts_answer(messages: List[Message]) -> str:
        """The tool results of the run, latest last, as a code-written note for the orchestrator."""
        results = [part.tool_response for m in messages for part in m.parts if part.tool_response]
        facts = json.dumps(results, ensure_ascii=False, default=str)
        if len(facts) > _FACTS_MAX_CHARS:
            facts = facts[:_FACTS_MAX_CHARS] + " …(cut)"
        return f"Tool note: the task lookup reached its time limit before a summary was written. Tool results: {facts}"

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, args: Dict[str, Any], user_id: str) -> Any:
        if name == "list_tasks":
            tasks = await self._tasks.list_tasks(
                user_id=user_id,
                show_completed=args.get("show_completed", False),
            )
            return self._list_page(tasks, args)

        if name == "search_tasks":
            entries = await self._indexing.search(
                user_id=user_id,
                query=args["query"],
                show_completed=args.get("show_completed", False),
            )
            if not entries:
                return {
                    "tasks": [],
                    "count": 0,
                    "note": (
                        f"No task is close to '{args['query']}'. Another wording of the same topic will not find "
                        "more: answer that no matching task was found, or read the tasks with list_tasks."
                    ),
                }
            refs: List[Tuple[str, str]] = [(e.list_id, e.task_id) for e in entries]
            tasks = await self._tasks.batch_get_tasks(user_id=user_id, task_refs=refs)
            return self._format_task_list(tasks)

        if name == "create_task":
            recurrence: Optional[TaskRecurrence] = None
            if rec_args := args.get("recurrence"):
                recurrence = self._parse_recurrence(rec_args, args.get("due_datetime"))
            parsed_due = self._parse_datetime(args.get("due_datetime"))
            reminder_dt, is_reminder_on = self._derive_reminder(
                self._parse_datetime(args.get("reminder_datetime")),
                parsed_due,
            )
            task = await self._tasks.create_task(
                user_id=user_id,
                task=TaskCreate(
                    title=args["title"],
                    body=args.get("body"),
                    due_datetime=parsed_due,
                    start_datetime=self._parse_datetime(args.get("start_datetime")),
                    reminder_datetime=reminder_dt,
                    is_reminder_on=is_reminder_on,
                    importance=TaskImportance(args["importance"]) if args.get("importance") else TaskImportance.NORMAL,
                    tags=args.get("tags") or [],
                    recurrence=recurrence,
                ),
            )
            await self._indexing.index_task(task)
            return {"created": True, "title": task.title}

        if name == "update_task":
            list_id, task_id = await self._indexing.resolve_short_id(user_id, args["task_ref"])
            parsed_due = self._parse_datetime(args.get("due_datetime"))
            parsed_reminder = self._parse_datetime(args.get("reminder_datetime"))
            # Auto-reminder: task had no due_date, a new due_datetime is set, no explicit reminder.
            reminder_dt = parsed_reminder
            is_reminder_on: Optional[bool] = args.get("is_reminder_on")
            if parsed_due is not None and parsed_reminder is None:
                current = await self._tasks.get_task(user_id, list_id, task_id)
                if current.due_datetime is None:
                    reminder_dt, is_reminder_on = self._derive_reminder(None, parsed_due)
            recurrence: Optional[TaskRecurrence] = None
            if rec_args := args.get("recurrence"):
                recurrence = self._parse_recurrence(rec_args, args.get("due_datetime"))
            task = await self._tasks.update_task(
                user_id=user_id,
                list_id=list_id,
                task_id=task_id,
                updates=TaskUpdate(
                    title=args.get("title"),
                    body=args.get("body"),
                    due_datetime=parsed_due,
                    start_datetime=self._parse_datetime(args.get("start_datetime")),
                    reminder_datetime=reminder_dt,
                    is_reminder_on=is_reminder_on,
                    importance=TaskImportance(args["importance"]) if args.get("importance") else None,
                    status=TaskStatus(args["status"]) if args.get("status") else None,
                    tags=args.get("tags"),
                    recurrence=recurrence,
                ),
            )
            await self._indexing.index_task(task)
            return {"updated": True, "title": task.title}

        if name == "delete_task":
            list_id, task_id = await self._indexing.resolve_short_id(user_id, args["task_ref"])
            await self._tasks.delete_task(
                user_id=user_id,
                list_id=list_id,
                task_id=task_id,
            )
            await self._indexing.deindex_task(user_id, task_id)
            return {"deleted": True}

        return {"error": f"unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_reminder(
        explicit_reminder: Optional[datetime],
        due_datetime: Optional[datetime],
    ) -> Tuple[Optional[datetime], bool]:
        """
        Return (reminder_datetime, is_reminder_on).

        Priority:
          1. explicit_reminder is set → use it as-is, is_reminder_on=True.
          2. due_datetime is set → auto-set to 8pm the day before, is_reminder_on=True.
          3. Neither → (None, False): no reminder.
        """
        if explicit_reminder is not None:
            return explicit_reminder, True
        if due_datetime is not None:
            auto = (due_datetime - timedelta(days=1)).replace(
                hour=20, minute=0, second=0, microsecond=0
            )
            return auto, True
        return None, False

    def _list_page(self, tasks: List[Task], args: Dict[str, Any]) -> Dict[str, Any]:
        """One page of `tasks` for list_tasks: the due-period filter, the order, the page and the counts that tell the
        model whether it has seen every task (module docstring, list_tasks flow)."""
        bounds: Dict[str, Optional[datetime]] = {}
        for key in ("due_from", "due_to"):
            raw = args.get(key)
            bounds[key] = self._parse_datetime(raw) if raw else None
            if raw and bounds[key] is None:
                return {"error": f"{key} is not an ISO-8601 date or date-time: {raw!r}"}
        start, stop = _as_utc(bounds["due_from"]), _as_utc(bounds["due_to"])
        if stop is not None and len(str(args["due_to"]).strip()) == 10:   # a date alone: that whole day
            stop = stop + timedelta(days=1) - timedelta(microseconds=1)
        filtered = start is not None or stop is not None
        def due(t: Task) -> datetime:
            return _as_utc(t.due_datetime) or _EPOCH

        if filtered:
            tasks = [
                t for t in tasks
                if t.due_datetime is not None
                and (start is None or due(t) >= start)
                and (stop is None or due(t) <= stop)
            ]
        dated = sorted((t for t in tasks if t.due_datetime is not None), key=due)
        undated = sorted((t for t in tasks if t.due_datetime is None),
                         key=lambda t: _as_utc(t.created_at) or _EPOCH, reverse=True)
        ordered = dated + undated

        total = len(ordered)
        pages = max(1, math.ceil(total / _PAGE_SIZE))
        try:
            page = max(1, int(args.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        chunk = ordered[(page - 1) * _PAGE_SIZE: page * _PAGE_SIZE]

        which = "tasks, open and completed" if args.get("show_completed") else "open tasks"
        scope = " due in that period" if filtered else ""
        if total == 0:
            note = f"There are no {which}{scope}."
        elif page > pages:
            note = f"Page {page} is past the last page ({pages}) of the {total} {which}{scope}."
        elif pages == 1:
            note = f"These are all {total} {which}{scope}: there is nothing more to list or search."
        elif page < pages:
            note = (f"Page {page} of {pages}: {total} {which}{scope}. list_tasks with page={page + 1} "
                    f"returns the next {_PAGE_SIZE}.")
        else:
            note = f"Page {page} of {pages}, the last: {total} {which}{scope}."
        result = self._format_task_list(chunk)
        result.update({"total": total, "page": page, "pages": pages, "note": note})
        return result

    @staticmethod
    def _short_id(task_id: str) -> str:
        import hashlib
        return hashlib.md5(task_id.encode()).hexdigest()[:8]

    def _format_task_list(self, tasks: List[Task]) -> Dict[str, Any]:
        if not tasks:
            return {"tasks": [], "count": 0}

        def _dt(v: Any) -> Optional[str]:
            return v.isoformat() if v else None

        def _serialize(t: Task) -> Dict[str, Any]:
            d: Dict[str, Any] = {
                "ref": self._short_id(t.task_id),
                "title": t.title,
                "status": t.status.value,
                "importance": t.importance.value,
            }
            if t.body:
                d["body"] = t.body
            if t.tags:
                d["tags"] = list(t.tags)
            if t.due_datetime:
                d["due_datetime"] = _dt(t.due_datetime)
            if t.reminder_datetime:
                d["reminder_datetime"] = _dt(t.reminder_datetime)
                d["is_reminder_on"] = t.is_reminder_on
            if t.completed_at:
                d["completed_at"] = _dt(t.completed_at)
            if t.checklist_items:
                d["checklist_items"] = [
                    {
                        "item_id": c.item_id,
                        "title": c.title,
                        "is_completed": c.is_completed,
                        **({"checked_at": _dt(c.checked_at)} if c.checked_at else {}),
                    }
                    for c in t.checklist_items
                ]
            if t.linked_resources:
                d["linked_resources"] = [
                    {
                        "display_name": r.display_name,
                        "web_url": r.web_url,
                        **({"application_name": r.application_name} if r.application_name else {}),
                    }
                    for r in t.linked_resources
                ]
            if t.recurrence:
                d["recurrence"] = {
                    "type": t.recurrence.pattern.type,
                    "interval": t.recurrence.pattern.interval,
                }
            if t.attachments:
                d["attachments"] = [
                    {
                        "attachment_id": a.attachment_id,
                        "filename": a.filename,
                        **({"url": a.url} if a.url else {}),
                        **({"gcs_uri": a.gcs_uri} if a.gcs_uri else {}),
                    }
                    for a in t.attachments
                ]
            return d

        return {"tasks": [_serialize(t) for t in tasks], "count": len(tasks)}

    def _parse_recurrence(
        self, rec_args: Dict[str, Any], due_datetime_str: Optional[str]
    ) -> TaskRecurrence:
        """
        Parse LLM recurrence args into a TaskRecurrence domain object.

        Supported patterns (all that MS To Do actually implements):
          daily           — interval only
          weekdays        — convenience alias → weekly Mon–Fri
          weekly          — daysOfWeek (defaults to weekday of due_datetime)
          absoluteMonthly — dayOfMonth (defaults to day of due_datetime)
          absoluteYearly  — dayOfMonth + month (defaults from due_datetime)
        """
        pattern_type = rec_args["pattern"]
        interval = rec_args.get("interval", 1)
        due_dt = self._parse_datetime(due_datetime_str) or datetime.now(timezone.utc)

        _WEEKDAY_NAMES = [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]

        days_of_week: list = []
        day_of_month: Optional[int] = None
        month: Optional[int] = None

        if pattern_type == "weekdays":
            pattern_type = "weekly"
            days_of_week = ["monday", "tuesday", "wednesday", "thursday", "friday"]
        elif pattern_type == "weekly":
            days_of_week = rec_args.get("days_of_week") or [_WEEKDAY_NAMES[due_dt.weekday()]]
        elif pattern_type == "absoluteMonthly":
            day_of_month = rec_args.get("day_of_month") or due_dt.day
        elif pattern_type == "absoluteYearly":
            day_of_month = rec_args.get("day_of_month") or due_dt.day
            month = rec_args.get("month") or due_dt.month

        return TaskRecurrence(
            pattern=RecurrencePattern(
                type=pattern_type,
                interval=interval,
                days_of_week=days_of_week,
                day_of_month=day_of_month,
                month=month,
            ),
            range=RecurrenceRange(
                type="noEnd",
                start_date=datetime.now(timezone.utc).date().isoformat(),
            ),
        )

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.rstrip("Z"))
        except ValueError:
            logger.warning(f"📋 TasksAgent: could not parse datetime '{value}', ignoring")
            return None
