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
from ..domain.agent_note import NoteCreate, NoteUpdate, ReminderRecurrence
from ..infrastructure.agent_config import NOTES as NOTES_CFG
from ..infrastructure.agent_manifest import Intent, NOTES as NOTES_DESCRIPTOR
from ..ports.agent_note_port import AgentNotePort
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.user_notification_service import UserNotificationService

_NOTES_SOFT_THRESHOLD = 20

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
                "recurrence": {
                    "type": "object",
                    "description": "Recurrence schedule. Use type='once' for one-time reminders (default). Only use repeating types when explicitly requested.",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["once", "hourly", "daily", "weekly", "monthly"],
                        },
                        "interval": {
                            "type": "integer",
                            "description": "Every N units. Default 1.",
                        },
                    },
                    "required": ["type"],
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
                    "type": "object",
                    "description": "New recurrence settings. Replaces existing. Omit to keep unchanged.",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["hourly", "daily", "weekly", "monthly"],
                        },
                        "interval": {
                            "type": "integer",
                            "description": "Every N units. Default 1.",
                        },
                    },
                    "required": ["type"],
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


def _parse_recurrence(args: Optional[Dict[str, Any]]) -> Optional[ReminderRecurrence]:
    if not args or not args.get("type") or args.get("type") == "once":
        return None
    return ReminderRecurrence(
        type=args["type"],
        interval=int(args.get("interval") or 1),
    )


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
        prompt_builder: Optional[PromptBuilderPort] = None,
        user_timezone: str = "UTC",
        notification_service: Optional["UserNotificationService"] = None,
    ) -> None:
        super().__init__(config)
        self._set_execution_context(execution_context)
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self._notes = notes_port
        self._prompt_builder = prompt_builder
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
                rec_str = f", repeats {n.recurrence.type}" if n.recurrence else ""
                lines.append(f"  - [{n.note_id}] \"{n.text}\" | fires: {due_str}{rec_str}")
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
        if name == "create_self_reminder":
            due = _parse_dt(args.get("due"), self._user_tz)
            if due is None:
                return {"error": "create_self_reminder requires 'due' field (ISO-8601 datetime)."}
            instruction = args.get("instruction", "")
            note = await self._notes.create_note(NoteCreate(
                user_id=user_id,
                text=args.get("text", ""),
                instruction=instruction,
                due=due,
                recurrence=_parse_recurrence(args.get("recurrence")),
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
                + (f" (repeats {note.recurrence.type})" if note.recurrence else ""),
            )
            return result

        if name == "update_self_reminder":
            note_id = str(args.get("note_id") or "")
            note = await self._notes.update_note(NoteUpdate(
                note_id=note_id,
                user_id=user_id,
                text=args.get("text"),
                instruction=args.get("instruction"),
                due=_parse_dt(args.get("due"), self._user_tz),
                recurrence=_parse_recurrence(args.get("recurrence")),
            ))
            await self._notify(
                user_id, account_id,
                f"📝 Reminder updated: \"{note.text}\" — {self._fmt_due(note.due)}",
            )
            return {"note_id": note.note_id, "status": "updated"}

        if name == "delete_self_reminder":
            note_id = str(args.get("note_id") or "")
            deleted = await self._notes.delete_note(note_id=note_id, user_id=user_id)
            if not deleted:
                return {"error": f"Reminder {note_id!r} not found or does not belong to this user."}
            await self._notify(user_id, account_id, f"🗑️ Reminder deleted: ID {note_id}")
            return {"note_id": note_id, "status": "deleted"}

        return {"error": f"unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fmt_due(self, due: datetime) -> str:
        """Format UTC datetime as user-local string for transparency notifications."""
        return due.astimezone(self._user_tz).strftime("%d %b %Y %H:%M %Z")

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
