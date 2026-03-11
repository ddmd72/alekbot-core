"""
TasksAgent — specialist agent for task management.

One intent: manage_user_tasks
  payload: {"query": "<delegation instruction from orchestrator>"}

The agent receives a natural-language delegation from the orchestrator and
autonomously selects the correct CRUD tool to execute. Biographical context
is injected so the agent can understand personal references in tasks.

Tool-calling loop (mirrors MapsSearchAgent pattern):
  1. Build system prompt with bio context via PromptBuilderPort.
  2. Pass 5 CRUD tool declarations to the LLM.
  3. LLM selects tool(s) — e.g. search_tasks first, then update_task.
  4. Execute each tool call against TasksProviderPort.
  5. Repeat until LLM produces a final text response or MAX_TURNS reached.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..agents.base_agent import BaseAgent
from ..domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentResponse
from ..domain.task import Task, TaskCreate, TaskStatus, TaskUpdate
from ..infrastructure.agent_config import TASKS as TASKS_CFG
from ..infrastructure.agent_manifest import Intent
from ..ports.llm_port import AgentExecutionContext, LLMRequest, Message, MessagePart
from ..ports.prompt_builder_port import PromptBuilderPort
from ..ports.tasks_provider_port import TasksProviderPort
from ..utils.logger import logger

_MAX_TURNS = 4  # Most operations need 1–2 tool calls; 4 is a safe ceiling.

# ---------------------------------------------------------------------------
# Tool declarations — passed to LLM on every call
# ---------------------------------------------------------------------------

_TOOL_DECLARATIONS: List[Dict[str, Any]] = [
    {
        "name": "list_tasks",
        "description": (
            "Return all tasks from the user's task list. "
            "Use show_completed=true to include completed tasks. "
            "Default: active tasks only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "show_completed": {
                    "type": "boolean",
                    "description": "If true, return completed tasks instead of active ones.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_tasks",
        "description": (
            "Search the user's task list by keyword. "
            "Use this instead of list_tasks when the delegation mentions a specific topic, "
            "name, or description. Returns matching tasks with their IDs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to search for in task titles and notes.",
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
                "notes": {
                    "type": "string",
                    "description": "Optional extra detail. Omit if none.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional due date in YYYY-MM-DD format. Omit if not mentioned.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "Modify an existing task — rename, reschedule, mark as done, or mark as not done. "
            "Requires task_id. If task_id is unknown, call search_tasks first to find it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to update (from list_tasks or search_tasks result).",
                },
                "title": {
                    "type": "string",
                    "description": "New title. Omit if not changing.",
                },
                "notes": {
                    "type": "string",
                    "description": "New notes. Omit if not changing.",
                },
                "due_date": {
                    "type": "string",
                    "description": "New due date in YYYY-MM-DD. Omit if not changing.",
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "needsAction"],
                    "description": "'completed' to mark done, 'needsAction' to mark undone. Omit if not changing.",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": (
            "Permanently remove a task from the list. "
            "Requires task_id. If unknown, call search_tasks first."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "ID of the task to delete.",
                },
            },
            "required": ["task_id"],
        },
    },
]


class TasksAgent(BaseAgent):
    """
    Specialist agent for task management.

    Receives a natural-language delegation from the orchestrator.
    Uses a tool-calling loop to select and execute the right CRUD operation.
    Biographical context is injected so the agent understands personal references.
    """

    TEMPERATURE = TASKS_CFG.temperature
    MAX_TOKENS = TASKS_CFG.max_tokens

    def __init__(
        self,
        config: AgentConfig,
        execution_context: AgentExecutionContext,
        prompt_builder: PromptBuilderPort,
        tasks_provider: TasksProviderPort,
        user_id: Optional[str] = None,
    ) -> None:
        super().__init__(config)
        self.execution_context = execution_context
        self._llm = execution_context.provider
        self.model_name = execution_context.model_name
        self.prompt_builder = prompt_builder
        self._tasks = tasks_provider
        self.user_id = user_id

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
        user_id = message.context.get("user_id") or self.user_id
        account_id = message.context.get("account_id")

        self._on_agent_start(query)
        start_time = time.time()

        # Build system prompt with biographical context
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
            logger.warning(f"📋 TasksAgent: prompt build failed ({exc}), proceeding without bio context")

        messages: List[Message] = [
            Message(role="user", parts=[MessagePart(text=query)])
        ]

        final_text = ""

        for turn in range(_MAX_TURNS):
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages,
                tools=_TOOL_DECLARATIONS,
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                disable_safety=True,
            )
            response = await self._call_llm(request, turn=turn + 1)

            if not response.tool_calls:
                final_text = response.text or ""
                break

            # Append model tool-call message
            messages.append(Message(
                role="model",
                raw_content=response.raw_content,
                parts=[MessagePart(tool_call=tc) for tc in response.tool_calls],
            ))

            # Execute tool calls
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

        else:
            logger.warning(f"📋 TasksAgent: max turns ({_MAX_TURNS}) reached, forcing format")
            request = LLMRequest(
                model_name=self.model_name,
                system_instruction=system_prompt,
                messages=messages + [
                    Message(role="user", parts=[MessagePart(text="Summarise the results concisely.")])
                ],
                tools=[],
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                disable_safety=True,
            )
            response = await self._call_llm(request, turn=_MAX_TURNS + 1)
            final_text = response.text or ""

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
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, args: Dict[str, Any], user_id: str) -> Any:
        """Dispatch an LLM tool call to TasksProviderPort."""
        if name == "list_tasks":
            tasks = await self._tasks.list_tasks(
                user_id=user_id,
                show_completed=args.get("show_completed", False),
            )
            return self._format_task_list(tasks)

        if name == "search_tasks":
            tasks = await self._tasks.search_tasks(
                user_id=user_id,
                query=args.get("query", ""),
            )
            return self._format_task_list(tasks)

        if name == "create_task":
            task = await self._tasks.create_task(
                user_id=user_id,
                task=TaskCreate(
                    title=args["title"],
                    notes=args.get("notes"),
                    due_date=self._parse_date(args.get("due_date")),
                ),
            )
            return {"created": True, "task_id": task.task_id, "display": task.to_display_string()}

        if name == "update_task":
            task = await self._tasks.update_task(
                user_id=user_id,
                task_id=args["task_id"],
                updates=TaskUpdate(
                    title=args.get("title"),
                    notes=args.get("notes"),
                    due_date=self._parse_date(args.get("due_date")),
                    status=TaskStatus(args["status"]) if args.get("status") else None,
                ),
            )
            return {"updated": True, "task_id": task.task_id, "display": task.to_display_string()}

        if name == "delete_task":
            await self._tasks.delete_task(user_id=user_id, task_id=args["task_id"])
            return {"deleted": True, "task_id": args["task_id"]}

        return {"error": f"unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_task_list(tasks: List[Task]) -> Dict[str, Any]:
        if not tasks:
            return {"tasks": [], "count": 0}
        return {
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "notes": t.notes,
                    "due_date": t.due_date.strftime("%Y-%m-%d") if t.due_date else None,
                    "status": t.status.value,
                }
                for t in tasks
            ],
            "count": len(tasks),
        }

    @staticmethod
    def _parse_date(value: Any) -> Optional[datetime]:
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            logger.warning(f"📋 TasksAgent: could not parse date '{value}', ignoring")
            return None
