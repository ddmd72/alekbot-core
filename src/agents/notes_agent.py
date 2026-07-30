"""
Notes Agent
===========

Specialist executor for proactive self-reminders — deferred instructions that fire
automatically via Cloud Scheduler, regardless of user activity.

Single intent: manage_self_reminders.
Receives a natural language query from the orchestrator, selects the right tool via
one LLM call, executes CRUD directly against AgentNotePort, and returns a brief status.

Tools:
  create_self_reminder  — text (label) + instruction (execution context) + due + optional recurrence
  update_self_reminder  — note_id + optional fields (PATCH semantics)
  delete_self_reminder  — note_id

Two-field model:
  text        — short display label (≤15 words), shown in active_reminders context block
  instruction — full execution context (no limit); this is the ONLY input when the reminder
                fires. Cloud Scheduler → WorkerHandler → UserNotificationService.notify(
                system_alert=instruction) → QuickAgent executes as a new conversation.

Context injection:
  - Orchestrator: sees active_reminders {} summary (text + fires datetime) via RouterAgent
  - NotesAgent: sees full active_reminders block (text + instruction + due) loaded in _run()
  - Biographical facts included (include_biographical=True)

Transparency: every mutation sends notify_raw() to the user's last active channel.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.agent_note import NoteCreate, NoteUpdate
from ..domain.task_complexity import TaskComplexity
from ..infrastructure.agent_config import NOTES as NOTES_CFG
from ..infrastructure.agent_manifest import Intent, NOTES as NOTES_DESCRIPTOR
from ..ports.agent_note_port import AgentNotePort
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.recurrence_port import RecurrencePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.user_notification_service import UserNotificationService

_NOTES_SOFT_THRESHOLD = 20

# One schedule language for both tools. RRULE (RFC 5545) rather than a fixed
# type+interval pair: "Tuesdays and Fridays", "08:00 and 20:00" and "last Sunday of
# the month" are all one reminder, not the duplicates a single-interval model forced.
_RECURRENCE_PARAM = {
    "type": "string",
    "description": (
        "Repeat schedule as an RFC 5545 RRULE, without DTSTART — 'due' is the anchor "
        "and the first fire. Omit for a one-time reminder (the default; use it unless "
        "repetition was asked for). Time of day comes from 'due' unless BYHOUR says "
        "otherwise. Examples: "
        "FREQ=DAILY — every day at the due time; "
        "FREQ=DAILY;INTERVAL=2 — every second day; "
        "FREQ=WEEKLY;BYDAY=TU,FR — every Tuesday and Friday; "
        "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO — every other Monday; "
        "FREQ=DAILY;BYHOUR=8,20;BYMINUTE=0 — twice a day, 08:00 and 20:00; "
        "FREQ=MONTHLY;BYDAY=-1SU — the last Sunday of each month; "
        "FREQ=MONTHLY;BYMONTHDAY=1,15 — the 1st and the 15th. "
        "COUNT and UNTIL are rejected — end a reminder by deleting it."
    ),
}

_TOOL_DECLARATIONS = [
    {
        "name": "create_self_reminder",
        "description": "Create a self-reminder that will fire at a specified time.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Short display label — ≤15 words. Shown in working memory context.",
                },
                "instruction": {
                    "type": "string",
                    "description": (
                        "Full execution context. This is what runs when the reminder fires — "
                        "write it as a complete, self-contained instruction with all necessary "
                        "context: what to do, why, any relevant details from the conversation. "
                        "No length limit."
                    ),
                },
                "due": {
                    "type": "string",
                    "description": "ISO-8601 datetime in the user's local time when to fire.",
                },
                "recurrence": _RECURRENCE_PARAM,
                "complexity": {
                    "type": "string",
                    "enum": ["small_talk", "simple_analytics", "deep_reasoning"],
                    "description": (
                        "Execution tier for Smart when this reminder fires. "
                        "small_talk — ECO, no thinking; pure notification with no analysis "
                        "(e.g. 'remind me to take medicine'). "
                        "simple_analytics — default; BALANCED + light thinking (most reminders). "
                        "deep_reasoning — PERFORMANCE + heavy thinking; only for instructions that "
                        "require multi-step analysis, research or synthesis "
                        "(e.g. 'run morning inbox briefing and summarise action items'). "
                        "Omit to use default (simple_analytics)."
                    ),
                },
            },
            "required": ["text", "instruction", "due"],
        },
    },
    {
        "name": "update_self_reminder",
        "description": (
            "Update fields of an existing self-reminder. "
            "PATCH semantics — only provided fields are changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "Reminder ID (epoch-ms string from working_memory).",
                },
                "text": {
                    "type": "string",
                    "description": "New display label (≤15 words). Omit to keep unchanged.",
                },
                "instruction": {
                    "type": "string",
                    "description": "New execution context. Omit to keep unchanged.",
                },
                "due": {
                    "type": "string",
                    "description": "New ISO-8601 due datetime in user's local time. Omit to keep unchanged.",
                },
                "recurrence": {
                    **_RECURRENCE_PARAM,
                    "description": (
                        "New repeat schedule, replacing the current one. Omit to keep it "
                        "unchanged; to stop repeating use clear_recurrence. "
                        + _RECURRENCE_PARAM["description"]
                    ),
                },
                "clear_recurrence": {
                    "type": "boolean",
                    "description": (
                        "True turns a repeating reminder into a one-time one (fires once "
                        "more at 'due', then is deleted). Cannot be combined with recurrence."
                    ),
                },
                "complexity": {
                    "type": "string",
                    "enum": ["small_talk", "simple_analytics", "deep_reasoning"],
                    "description": "New execution tier. Omit to keep unchanged.",
                },
            },
            "required": ["note_id"],
        },
    },
    {
        "name": "delete_self_reminder",
        "description": "Delete a self-reminder by ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "Reminder ID (epoch-ms string from working_memory).",
                },
            },
            "required": ["note_id"],
        },
    },
]


def _resolve_tz(timezone_str: Optional[str]) -> ZoneInfo:
    """Resolve IANA timezone string to ZoneInfo. Falls back to UTC on invalid input."""
    if not timezone_str:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(timezone_str)
    except (ZoneInfoNotFoundError, KeyError):
        logger.warning("⚠️ [NotesAgent] Unknown timezone %r, falling back to UTC", timezone_str)
        return ZoneInfo("UTC")


def _parse_dt(value: Optional[str], user_tz: ZoneInfo) -> Optional[datetime]:
    """
    Parse ISO-8601 string to UTC datetime.

    - If the string has timezone info → convert to UTC.
    - If naive (no tz) → interpret as user's local time, then convert to UTC.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            # Naive: assume user's local timezone
            dt = dt.replace(tzinfo=user_tz)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        logger.warning("⚠️ [NotesAgent] Could not parse datetime: %r", value)
        return None


def _parse_complexity(value: Optional[str]) -> Optional[TaskComplexity]:
    if not value:
        return None
    try:
        return TaskComplexity(value)
    except ValueError:
        return None


class NotesAgent(BaseAgent):
    """
    Specialist for self-reminders — deferred instructions that fire proactively.
    One LLM call to parse natural language → CRUD via AgentNotePort.
    """

    _descriptor = NOTES_DESCRIPTOR

    TEMPERATURE = NOTES_CFG.temperature
    MAX_TOKENS = NOTES_CFG.max_tokens
    MAX_TURNS = NOTES_CFG.max_turns

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        notes_port: AgentNotePort,
        recurrence: RecurrencePort,
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_timezone: str = "UTC",
        notification_service: Optional["UserNotificationService"] = None,
    ) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._notes = notes_port
        self._recurrence = recurrence
        self._prompt_builder = prompt_builder
        self._user_tz_name = user_timezone or "UTC"
        self._user_tz = _resolve_tz(user_timezone)
        self._notification_service = notification_service

    async def can_handle(self, message: AgentMessage) -> bool:
        if message.intent != AgentIntent.QUERY:
            return False
        return message.payload.get("intent") == Intent.MANAGE_SELF_REMINDERS

    async def execute(self, message: AgentMessage) -> AgentResponse:
        query = message.payload.get("query", "")
        user_id = message.context.get("user_id") or ""
        account_id = message.context.get("account_id") or ""

        self._on_agent_start(query[:60])
        start_time = time.time()

        result = await self._run(query, user_id, account_id)

        duration_ms = int((time.time() - start_time) * 1000)

        if "error" in result:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=result["error"],
            )

        summary = result.get("summary", "done")
        self._on_agent_success(len(summary), 0, summary)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=summary,
            confidence=1.0,
            metadata={"duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _run(self, query: str, user_id: str, account_id: str) -> Dict[str, Any]:
        if not self._prompt_builder:
            raise ValueError("NotesAgent requires prompt_builder")
        system_prompt = await self._prompt_builder.build_for_agent(
            agent_type="notes",
            user_id=user_id,
            account_id=account_id,
            include_biographical=True,
        )

        active_notes = await self._notes.list_active_notes(user_id, as_of=datetime.now(timezone.utc))
        if active_notes:
            lines = []
            for n in active_notes:
                due_str = n.due.astimezone(self._user_tz).strftime("%Y-%m-%d %H:%M %Z") if n.due else "no due"
                # Every stored field the tools can write, so an edit is never blind:
                # the rule verbatim (it is what update_self_reminder takes), the
                # execution tier, and the last fire.
                rec_str = f", rrule: {n.recurrence}" if n.recurrence else ""
                cx_str = f", complexity: {n.complexity.value}" if n.complexity else ""
                fired_str = (
                    f", last fired: {n.last_fired.astimezone(self._user_tz).strftime('%Y-%m-%d %H:%M %Z')}"
                    if n.last_fired else ""
                )
                lines.append(
                    f"  - [{n.note_id}] \"{n.text}\" | fires: {due_str}"
                    f"{rec_str}{cx_str}{fired_str}"
                )
                lines.append(f"    instruction: {n.instruction}")
            system_prompt += "\n\nactive_reminders {\n" + "\n".join(lines) + "\n}"

        # Build tool declarations: CRUD tools + delegation tool (if coordinator available)
        tools = list(_TOOL_DECLARATIONS)
        if self.coordinator:
            available = self.coordinator.get_available_intents_for(self._descriptor)
            if available:
                tools.append(self._build_delegate_tool_declaration(available))

        messages = [Message(role="user", parts=[MessagePart(text=query or "(no instruction)")])]

        for turn in range(self.MAX_TURNS):
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                tools=tools,
                max_tokens=self.MAX_TOKENS,
                temperature=self.TEMPERATURE,
            )
            response = await self._call_llm(request, turn=turn)

            # Text response (no tool calls) — LLM is returning an error or summary
            if not response.tool_calls:
                text = response.text or ""
                if text:
                    return {"summary": text}
                return {"error": "LLM did not select a tool or provide a response."}

            # Process ALL tool calls in this response
            has_delegation = False
            crud_results = []

            # Execute all tool calls, collect results
            tool_results = []
            for tc in response.tool_calls:
                if tc.name == "delegate_to_specialist":
                    has_delegation = True
                    args = tc.args or {}
                    intent = args.get("intent", "")
                    delegate_query = args.get("query", "")
                    self._on_delegation(intent, delegate_query)
                    delegate_response = await self.coordinator.handle_delegation(
                        intent=intent,
                        query=delegate_query,
                        context={"user_id": user_id, "account_id": account_id},
                        calling_agent_id=self.agent_id,
                    )
                    result_text = str(delegate_response.result) if delegate_response.result else "No result"
                    tool_results.append((tc, result_text))
                else:
                    result = await self._execute_tool(tc.name, tc.args or {}, user_id, account_id)
                    crud_results.append(result)
                    tool_results.append((tc, str(result)))

            # Append formatted tool turn to message history
            messages.extend(self._build_tool_turn(response, tool_results))

            if crud_results:
                # Feed results back to LLM for a text summary (no tools — force text response)
                summary_request = LLMRequest(
                    model_name=self.model_name,
                    system_instruction=system_prompt,
                    messages=messages,
                    max_tokens=self.MAX_TOKENS,
                    temperature=self.TEMPERATURE,
                )
                summary_response = await self._call_llm(summary_request, turn=turn + 1)
                summary = summary_response.text or "Operation completed."
                # Check for errors in any CRUD result
                errors = [r.get("error") for r in crud_results if isinstance(r, dict) and r.get("error")]
                if errors:
                    return {"error": "; ".join(errors)}
                return {"summary": summary}

            if has_delegation:
                continue  # next turn — LLM will use delegation results

        return {"error": "Max turns exceeded without completing the operation."}

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(
        self, name: str, args: Dict[str, Any], user_id: str, account_id: str
    ) -> Any:
        if args.get("_parse_error") == "truncated_json":
            return {"error": (
                f"Tool arguments for {name} were truncated by the model API "
                f"(likely max_tokens reached during JSON generation). "
                f"Retry with a shorter 'instruction' or 'text' field."
            )}

        if name == "create_self_reminder":
            due = _parse_dt(args.get("due"), self._user_tz)
            if due is None:
                return {"error": "create_self_reminder requires 'due' field (ISO-8601 datetime)."}
            try:
                rule = self._normalize_rule(args.get("recurrence"))
            except ValueError as exc:
                return {"error": str(exc)}
            if rule:
                snapped = self._recurrence.first_occurrence(
                    rule, not_before=due, tz=self._user_tz_name
                )
                if snapped is None:
                    return {"error": f"Recurrence {rule!r} yields no occurrence after the due date."}
                # "Tue and Fri" proposed with a Wednesday due must fire Friday — the
                # rule owns the schedule, the proposed due only says "not before".
                due = snapped
            instruction = args.get("instruction", "")
            note = await self._notes.create_note(NoteCreate(
                user_id=user_id,
                text=args.get("text", ""),
                instruction=instruction,
                due=due,
                recurrence=rule,
                complexity=_parse_complexity(args.get("complexity")),
            ))
            result: Dict[str, Any] = {"note_id": note.note_id, "status": "created"}
            active = await self._notes.list_active_notes(user_id, as_of=datetime.now(timezone.utc))
            if len(active) >= _NOTES_SOFT_THRESHOLD:
                result["alert"] = (
                    f"You now have {len(active)} active reminders "
                    f"(soft cap: {_NOTES_SOFT_THRESHOLD}). "
                    "Review working_memory and delete stale reminders."
                )
            await self._notify(
                user_id, account_id,
                f"📌 Reminder set: \"{note.text}\" — {self._fmt_due(note.due)}"
                + self._fmt_recurrence(note.recurrence),
            )
            return result

        if name == "update_self_reminder":
            note_id = str(args.get("note_id") or "").strip()
            if not note_id:
                return {"error": "update_self_reminder requires non-empty 'note_id'. Read it from active_reminders block; never fabricate or omit."}
            clear_recurrence = bool(args.get("clear_recurrence"))
            try:
                rule = self._normalize_rule(args.get("recurrence"))
            except ValueError as exc:
                return {"error": str(exc)}
            if clear_recurrence and rule:
                return {"error": "Pass either 'recurrence' or 'clear_recurrence', not both."}

            due = _parse_dt(args.get("due"), self._user_tz)
            if rule:
                # A new rule must own the schedule, so the fire time is snapped onto
                # it — against the new due when given, otherwise the stored one.
                anchor = due or await self._current_due(note_id, user_id)
                if anchor is None:
                    return {"error": f"Reminder {note_id!r} not found."}
                snapped = self._recurrence.first_occurrence(
                    rule, not_before=anchor, tz=self._user_tz_name
                )
                if snapped is None:
                    return {"error": f"Recurrence {rule!r} yields no occurrence after the due date."}
                due = snapped

            note = await self._notes.update_note(NoteUpdate(
                note_id=note_id,
                user_id=user_id,
                text=args.get("text"),
                instruction=args.get("instruction"),
                due=due,
                recurrence=rule,
                complexity=_parse_complexity(args.get("complexity")),
                clear_recurrence=clear_recurrence,
            ))
            await self._notify(
                user_id, account_id,
                f"📝 Reminder updated: \"{note.text}\" — {self._fmt_due(note.due)}"
                + self._fmt_recurrence(note.recurrence),
            )
            return {"note_id": note.note_id, "status": "updated"}

        if name == "delete_self_reminder":
            note_id = str(args.get("note_id") or "").strip()
            if not note_id:
                return {"error": "delete_self_reminder requires non-empty 'note_id'. Read it from active_reminders block; never fabricate or omit."}
            deleted = await self._notes.delete_note(note_id=note_id, user_id=user_id)
            if not deleted:
                return {"error": f"Reminder {note_id!r} not found or does not belong to this user."}
            await self._notify(user_id, account_id, f"🗑️ Reminder deleted: ID {note_id}")
            return {"note_id": note_id, "status": "deleted"}

        return {"error": f"unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _normalize_rule(self, raw: Any) -> Optional[str]:
        """Tool argument → canonical RRULE, or None when no schedule was given.

        Raises ``ValueError`` carrying the port's reason — returned to the LLM as a
        tool error so it can correct the rule instead of silently storing a broken one.
        """
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return self._recurrence.normalize(text)
        except ValueError as exc:
            raise ValueError(f"Invalid recurrence rule {text!r}: {exc}") from exc

    async def _current_due(self, note_id: str, user_id: str) -> Optional[datetime]:
        note = await self._notes.get_note(user_id=user_id, note_id=note_id)
        return note.due if note else None

    def _fmt_due(self, due: datetime) -> str:
        """Format UTC datetime as user-local string for transparency notifications."""
        return due.astimezone(self._user_tz).strftime("%d %b %Y %H:%M %Z")

    def _fmt_recurrence(self, rule: Optional[str]) -> str:
        """User-facing schedule suffix — phrased, never the raw rule."""
        return f" (repeats {self._recurrence.describe(rule)})" if rule else ""

    async def _notify(self, user_id: str, account_id: str, text: str) -> None:
        """Best-effort transparency notification — failure is logged and swallowed."""
        if not self._notification_service:
            return
        try:
            await self._notification_service.notify_raw(
                user_id=user_id,
                account_id=account_id,
                text=text,
            )
        except Exception as exc:
            logger.warning("⚠️ [NotesAgent] notify_raw failed: %s", exc)
