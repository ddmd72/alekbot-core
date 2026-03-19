# RFC: Tasks Specialist Agent (Google Tasks, provider-agnostic port)

**Status:** ✅ IMPLEMENTED
**Superseded by:** `TASKS_LOCAL_FIRST_RFC.md` (2026-03-18) — Microsoft To Do implementation
**Date:** 2026-03-08
**Implemented:** 2026-03-09

## Implementation Delta vs. RFC

The implementation diverges from the RFC in one important architectural decision made during implementation:

### Single intent instead of 5 intents

**RFC proposed:** 5 intents — `create_task`, `list_tasks`, `update_task`, `delete_task`, `search_tasks`

**Implemented:** 1 intent — `manage_user_tasks`

**Rationale:** The orchestrator does not need to know which CRUD operation to perform. It delegates a natural-language instruction ("Find tasks about milk", "Delete the Roast Alek task") and the `TasksAgent` autonomously selects the right tool via a tool-calling loop (max 4 turns). This eliminates a fragile mapping layer in the orchestrator and moves intelligence where it belongs — in the specialist agent.

**Key behaviours that emerge from this design:**
- The LLM executes `search_tasks` before `update_task`/`delete_task` when no `task_id` is provided — without any explicit orchestrator instruction to do so.
- On first delete attempt with an overly-specific search query returning 0 results, the LLM retries with a broader query — self-correcting without a hard-coded retry rule.
- The orchestrator can parallelize multiple task operations in a single user message (e.g. "add X, add Y, delete Z, show list") by issuing 4 parallel `delegate_to_specialist` calls.

### Architecture diagram (actual)

```
Slack/Telegram
    ↓
QuickResponseAgent / SmartResponseAgent
    ↓ intent: manage_user_tasks
    ↓ query: "<natural language delegation>"
TasksAgent(BaseAgent)
    ↓ Build system prompt with bio context (PromptBuilderPort)
    ↓ Tool-calling loop (max 4 turns):
    ↓   LLM selects tool → execute against TasksProviderPort → repeat
    ↓   Tools: list_tasks | search_tasks | create_task | update_task | delete_task
GoogleTasksAdapter(TasksProviderPort)
    ↓ Resolves OAuthCredentials via OAuthCredentialsPort (provider="google_tasks")
    ↓ Auto-refreshes token if < 5 min from expiry
    ↓ Manages dedicated tasklist "Alek Bot Tasks" (created on first use, ID cached in memory)
    → Google Tasks REST API
```

---

---

## 1. Context & Motivation

User manages tasks via Google Tasks. Wants a specialist agent that handles CRUD
operations on tasks through natural language in Slack. First provider: Google Tasks.
Second provider in view: Things 3 / Apple Reminders.

Port design constraint: Things 3 has no REST API with OAuth — it uses a local HTTP
server with a token. Therefore `OAuthCredentials` must NOT flow through port method
parameters. Each adapter resolves auth internally via `OAuthCredentialsPort` injected
at constructor time. Port methods carry only task data + `user_id`.

Agent constraint: manages a **single dedicated tasklist** per user ("Alek Bot Tasks"),
not all lists. The adapter creates the list on first use and stores its ID in Firestore.

---

## 2. Architecture Overview

```
Slack/Telegram
    ↓
QuickResponseAgent / SmartResponseAgent
    ↓ intent: create_task / list_tasks / update_task / delete_task / search_tasks
TasksAgent(BaseAgent)
    ↓ 1. LLM extracts structured params from natural language
    ↓ 2. Calls TasksProviderPort method
GoogleTasksAdapter(TasksProviderPort)
    ↓ fetches OAuthCredentials from OAuthCredentialsPort by user_id
    ↓ calls Google Tasks REST API
    → Returns Task list / created Task
```

---

## 3. Files Overview

### New files (9)
| File | Lines (est.) |
|------|-------------|
| `src/domain/task.py` | ~80 |
| `src/ports/tasks_provider_port.py` | ~60 |
| `src/adapters/google_tasks_adapter.py` | ~280 |
| `src/agents/tasks_agent.py` | ~270 |
| `tests/unit/agents/test_tasks_agent.py` | ~200 |
| `tests/unit/ports/test_tasks_provider_port.py` | ~50 |
| `firestore_utils/uploads/COGNITIVE_PROCESS_TASKS.groovy` | ~80 |
| `firestore_utils/uploads/tasks_agent_v1.json` | ~20 |
| `firestore_utils/uploads/tasks.json` | ~20 |

### Modified files (7)
| File | Change |
|------|--------|
| `src/infrastructure/agent_manifest.py` | +5 Intent constants, +TASKS descriptor, +ALL_DESCRIPTORS |
| `src/infrastructure/agent_config.py` | +TasksAgentConfig dataclass + TASKS singleton |
| `src/services/agent_context_builder.py` | +"tasks" entry in STRATEGIES dict |
| `src/services/gmail_oauth_service.py` | rename class `GmailOAuthService` → `GoogleOAuthService`; rename file to `google_oauth_service.py` |
| `src/composition/service_container.py` | +GoogleTasksAdapter init, +tasks_provider in agent_services() |
| `src/composition/user_agent_factory.py` | +4 touch points (import, build context, instantiate, register) |
| `src/web/oauth_app.py` | +2 endpoints (connect-google-tasks + callback) |

---

## 4. Implementation: New Files

### 4.1 `src/domain/task.py`

```python
"""
Task domain models.

Used by TasksProviderPort and TasksAgent.
Supports Google Tasks (first provider) and Things 3 / Apple Reminders (future).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class TaskStatus(str, Enum):
    NEEDS_ACTION = "needsAction"
    COMPLETED = "completed"


class Task(BaseModel):
    """A task from any task management provider."""

    task_id: str
    title: str
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    status: TaskStatus = TaskStatus.NEEDS_ACTION
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    provider: str = "google_tasks"  # "google_tasks" | "things3" | "apple_reminders"

    def is_completed(self) -> bool:
        return self.status == TaskStatus.COMPLETED

    def to_display_string(self) -> str:
        """Single-line summary for Slack output."""
        status_icon = "✅" if self.is_completed() else "⬜"
        due = f" (due {self.due_date.strftime('%Y-%m-%d')})" if self.due_date else ""
        notes = f" — {self.notes[:80]}" if self.notes else ""
        return f"{status_icon} {self.title}{due}{notes}"


class TaskCreate(BaseModel):
    """Input for creating a new task."""

    title: str
    notes: Optional[str] = None
    due_date: Optional[datetime] = None


class TaskUpdate(BaseModel):
    """Input for updating an existing task. All fields optional."""

    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[TaskStatus] = None
```

---

### 4.2 `src/ports/tasks_provider_port.py`

```python
"""
TasksProviderPort — abstract interface for task management providers.

Port is justified: 2+ planned implementations
  (1) GoogleTasksAdapter — REST API + OAuth
  (2) Things3Adapter — local HTTP API with token (future)

Design note: OAuthCredentials are NOT passed via method parameters.
Each adapter resolves auth internally by user_id, using OAuthCredentialsPort
injected at construction time. This makes the port compatible with both
OAuth-based (Google) and token-based (Things 3) providers.
"""

from abc import ABC, abstractmethod
from typing import List

from ..domain.task import Task, TaskCreate, TaskUpdate


class TasksProviderPort(ABC):
    """Abstract interface for task management providers."""

    @abstractmethod
    async def list_tasks(
        self,
        user_id: str,
        show_completed: bool = False,
    ) -> List[Task]:
        """
        List all tasks in the user's dedicated tasklist.

        Args:
            user_id: User identifier (used to fetch credentials internally).
            show_completed: If True, include completed tasks.

        Returns:
            List of Task objects, ordered by due_date ascending (null last).
        """
        ...

    @abstractmethod
    async def create_task(
        self,
        user_id: str,
        task: TaskCreate,
    ) -> Task:
        """
        Create a new task in the user's dedicated tasklist.

        Returns the created Task with provider-assigned task_id.
        """
        ...

    @abstractmethod
    async def update_task(
        self,
        user_id: str,
        task_id: str,
        updates: TaskUpdate,
    ) -> Task:
        """
        Update an existing task.

        Raises ValueError if task_id not found.
        Returns the updated Task.
        """
        ...

    @abstractmethod
    async def delete_task(
        self,
        user_id: str,
        task_id: str,
    ) -> None:
        """
        Delete a task by ID.

        Raises ValueError if task_id not found.
        """
        ...

    @abstractmethod
    async def search_tasks(
        self,
        user_id: str,
        query: str,
    ) -> List[Task]:
        """
        Search tasks by keyword in title and notes.

        Implementation note: Google Tasks API has no full-text search.
        Implementations should call list_tasks() and filter client-side.

        Returns tasks where query appears in title or notes (case-insensitive).
        """
        ...
```

---

### 4.3 `src/adapters/google_tasks_adapter.py`

```python
"""
GoogleTasksAdapter — implements TasksProviderPort using Google Tasks REST API.

Auth strategy:
  - Fetches OAuthCredentials from OAuthCredentialsPort by user_id
    (provider="google_tasks")
  - Refreshes access_token when expired (same pattern as GmailProviderAdapter)

Dedicated list:
  - Each user has one list named "Alek Bot Tasks"
  - List ID is stored in Firestore (via OAuthCredentialsPort as metadata,
    OR in a dedicated Firestore doc — see _get_or_create_tasklist())
  - List is created on first use

Google Tasks API base: https://tasks.googleapis.com/tasks/v1/
OAuth scope required: https://www.googleapis.com/auth/tasks
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiohttp

from ..domain.task import Task, TaskCreate, TaskStatus, TaskUpdate
from ..ports.tasks_provider_port import TasksProviderPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..domain.email import OAuthCredentials  # Reuse existing domain model
from ..utils.logger import logger

_TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TASKLIST_NAME = "Alek Bot Tasks"
_PROVIDER = "google_tasks"

# Concurrent request semaphore
_FETCH_SEMAPHORE_LIMIT = 5


class GoogleTasksAdapter(TasksProviderPort):
    """
    Implements TasksProviderPort for Google Tasks REST API.

    Constructor receives OAuthCredentialsPort (to fetch user tokens by user_id)
    and Google OAuth2 client credentials (for token refresh).

    The adapter manages a single dedicated tasklist per user ("Alek Bot Tasks").
    Tasklist IDs are cached in memory per instance (worker-level cache).
    """

    def __init__(
        self,
        oauth_credentials_repo: OAuthCredentialsPort,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._credentials_repo = oauth_credentials_repo
        self._client_id = client_id
        self._client_secret = client_secret
        # In-memory cache: user_id → tasklist_id (avoids Firestore lookup on every call)
        self._tasklist_cache: Dict[str, str] = {}
        logger.info("✅ GoogleTasksAdapter initialized")

    # ------------------------------------------------------------------
    # TasksProviderPort implementation
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        user_id: str,
        show_completed: bool = False,
    ) -> List[Task]:
        credentials = await self._get_fresh_credentials(user_id)
        tasklist_id = await self._get_or_create_tasklist(user_id, credentials)
        headers = {"Authorization": f"Bearer {credentials.access_token}"}

        params: Dict = {"showCompleted": "true" if show_completed else "false"}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_TASKS_BASE}/lists/{tasklist_id}/tasks",
                headers=headers,
                params=params,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        items = data.get("items", [])
        tasks = [self._parse_task(item) for item in items]

        logger.info(
            f"📋 GoogleTasksAdapter.list_tasks: {len(tasks)} tasks "
            f"for user={user_id[:8]} show_completed={show_completed}"
        )
        return tasks

    async def create_task(
        self,
        user_id: str,
        task: TaskCreate,
    ) -> Task:
        credentials = await self._get_fresh_credentials(user_id)
        tasklist_id = await self._get_or_create_tasklist(user_id, credentials)
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Content-Type": "application/json",
        }

        body: Dict = {"title": task.title}
        if task.notes:
            body["notes"] = task.notes
        if task.due_date:
            # Google Tasks expects RFC 3339 format with Z suffix
            body["due"] = task.due_date.strftime("%Y-%m-%dT00:00:00.000Z")

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_TASKS_BASE}/lists/{tasklist_id}/tasks",
                headers=headers,
                json=body,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        created = self._parse_task(data)
        logger.info(
            f"📋 GoogleTasksAdapter.create_task: '{task.title}' "
            f"id={created.task_id} user={user_id[:8]}"
        )
        return created

    async def update_task(
        self,
        user_id: str,
        task_id: str,
        updates: TaskUpdate,
    ) -> Task:
        credentials = await self._get_fresh_credentials(user_id)
        tasklist_id = await self._get_or_create_tasklist(user_id, credentials)
        headers = {
            "Authorization": f"Bearer {credentials.access_token}",
            "Content-Type": "application/json",
        }

        # PATCH body: only include fields that are being updated
        body: Dict = {}
        if updates.title is not None:
            body["title"] = updates.title
        if updates.notes is not None:
            body["notes"] = updates.notes
        if updates.due_date is not None:
            body["due"] = updates.due_date.strftime("%Y-%m-%dT00:00:00.000Z")
        if updates.status is not None:
            body["status"] = updates.status.value
            if updates.status == TaskStatus.COMPLETED:
                body["completed"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{_TASKS_BASE}/lists/{tasklist_id}/tasks/{task_id}",
                headers=headers,
                json=body,
            ) as resp:
                if resp.status == 404:
                    raise ValueError(f"Task {task_id} not found")
                resp.raise_for_status()
                data = await resp.json()

        updated = self._parse_task(data)
        logger.info(
            f"📋 GoogleTasksAdapter.update_task: {task_id} user={user_id[:8]}"
        )
        return updated

    async def delete_task(
        self,
        user_id: str,
        task_id: str,
    ) -> None:
        credentials = await self._get_fresh_credentials(user_id)
        tasklist_id = await self._get_or_create_tasklist(user_id, credentials)
        headers = {"Authorization": f"Bearer {credentials.access_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{_TASKS_BASE}/lists/{tasklist_id}/tasks/{task_id}",
                headers=headers,
            ) as resp:
                if resp.status == 404:
                    raise ValueError(f"Task {task_id} not found")
                resp.raise_for_status()

        logger.info(
            f"📋 GoogleTasksAdapter.delete_task: {task_id} user={user_id[:8]}"
        )

    async def search_tasks(
        self,
        user_id: str,
        query: str,
    ) -> List[Task]:
        """Client-side filter: Google Tasks has no server-side full-text search."""
        all_tasks = await self.list_tasks(user_id, show_completed=False)
        query_lower = query.lower()
        results = [
            t for t in all_tasks
            if query_lower in t.title.lower()
            or (t.notes and query_lower in t.notes.lower())
        ]
        logger.info(
            f"📋 GoogleTasksAdapter.search_tasks: '{query}' → {len(results)} results "
            f"user={user_id[:8]}"
        )
        return results

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _get_fresh_credentials(self, user_id: str) -> OAuthCredentials:
        """
        Fetch OAuthCredentials from Firestore. Refresh if expired.
        Raises ValueError if no credentials found for user.
        """
        creds = await self._credentials_repo.get_credentials(
            user_id=user_id, provider=_PROVIDER
        )
        if not creds:
            raise ValueError(
                f"No Google Tasks credentials found for user {user_id[:8]}. "
                "Please connect Google Tasks via /auth/connect-google-tasks"
            )

        # Refresh if token expires within 5 minutes
        now = datetime.utcnow()
        if creds.token_expiry and (creds.token_expiry - now).total_seconds() < 300:
            creds = await self._refresh_token(creds)
            await self._credentials_repo.save_credentials(creds)

        return creds

    async def _refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        """Exchange refresh_token for a new access_token."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": credentials.refresh_token,
                    "grant_type": "refresh_token",
                },
            ) as resp:
                data = await resp.json()

        if "error" in data:
            raise ValueError(
                f"Google Tasks token refresh failed: {data['error']} — "
                f"{data.get('error_description', '')}"
            )

        logger.info(f"🔑 Google Tasks token refreshed for user={credentials.user_id[:8]}")
        return OAuthCredentials(
            user_id=credentials.user_id,
            provider=credentials.provider,
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token") or credentials.refresh_token,
            token_expiry=datetime.utcnow() + timedelta(seconds=data["expires_in"]),
            scopes=credentials.scopes,
            email_address=credentials.email_address,
        )

    # ------------------------------------------------------------------
    # Tasklist management
    # ------------------------------------------------------------------

    async def _get_or_create_tasklist(
        self, user_id: str, credentials: OAuthCredentials
    ) -> str:
        """
        Returns the ID of the user's dedicated tasklist.
        Checks in-memory cache first, then searches Google Tasks,
        then creates the list if it doesn't exist.
        """
        if user_id in self._tasklist_cache:
            return self._tasklist_cache[user_id]

        headers = {"Authorization": f"Bearer {credentials.access_token}"}

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_TASKS_BASE}/users/@me/lists",
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        for item in data.get("items", []):
            if item.get("title") == _TASKLIST_NAME:
                list_id = item["id"]
                self._tasklist_cache[user_id] = list_id
                logger.info(
                    f"📋 Found existing tasklist '{_TASKLIST_NAME}' "
                    f"id={list_id} user={user_id[:8]}"
                )
                return list_id

        # Not found — create it
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_TASKS_BASE}/users/@me/lists",
                headers={**headers, "Content-Type": "application/json"},
                json={"title": _TASKLIST_NAME},
            ) as resp:
                resp.raise_for_status()
                created = await resp.json()

        list_id = created["id"]
        self._tasklist_cache[user_id] = list_id
        logger.info(
            f"📋 Created tasklist '{_TASKLIST_NAME}' id={list_id} user={user_id[:8]}"
        )
        return list_id

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_task(item: dict) -> Task:
        """Convert Google Tasks API item dict to Task domain model."""
        status = (
            TaskStatus.COMPLETED
            if item.get("status") == "completed"
            else TaskStatus.NEEDS_ACTION
        )

        due_date: Optional[datetime] = None
        if item.get("due"):
            try:
                due_date = datetime.strptime(
                    item["due"][:10], "%Y-%m-%d"
                )
            except (ValueError, TypeError):
                pass

        created_at: Optional[datetime] = None
        if item.get("updated"):
            try:
                created_at = datetime.strptime(
                    item["updated"][:19], "%Y-%m-%dT%H:%M:%S"
                )
            except (ValueError, TypeError):
                pass

        return Task(
            task_id=item["id"],
            title=item.get("title", ""),
            notes=item.get("notes"),
            due_date=due_date,
            status=status,
            created_at=created_at,
            updated_at=created_at,
            provider=_PROVIDER,
        )
```

---

### 4.4 `src/agents/tasks_agent.py`

```python
"""
TasksAgent — specialist agent for task management.

Five intents (all routed through one agent instance):

  list_tasks       — list all tasks (optionally show completed)
                     payload: {"query": "show my tasks" | "show completed"}

  create_task      — create a new task
                     payload: {"query": "create task: buy milk tomorrow"}

  update_task      — update title/notes/due_date or mark as complete/incomplete
                     payload: {"query": "mark buy milk as done",
                               "task_id": "<id from prior list>"}  # optional

  delete_task      — delete a task by natural description or ID
                     payload: {"query": "delete buy milk",
                               "task_id": "<id>"}  # optional

  search_tasks     — keyword search in title/notes
                     payload: {"query": "find tasks about groceries"}

Routing: execute() dispatches on message.intent directly.
LLM is called only for intents requiring param extraction (create, update).
list_tasks, delete_task, search_tasks — direct API calls with optional LLM assist.
"""

from __future__ import annotations

import json
import time
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


class TasksAgent(BaseAgent):
    """
    Specialist agent for task management (Google Tasks, first provider).

    Delegates to TasksProviderPort — no direct knowledge of provider.
    LLM is used only for natural language → structured params extraction.
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
        valid_intents = {
            Intent.LIST_TASKS, Intent.CREATE_TASK, Intent.UPDATE_TASK,
            Intent.DELETE_TASK, Intent.SEARCH_TASKS,
        }
        # Intents are embedded in the payload as "intent" key by orchestrators
        payload_intent = message.payload.get("intent", "")
        return payload_intent in valid_intents or bool(message.payload.get("query"))

    async def execute(self, message: AgentMessage) -> AgentResponse:
        """Dispatch to handler based on message.intent."""
        intent = message.payload.get("intent", "")
        user_id = message.context.get("user_id") or self.user_id

        dispatch = {
            Intent.LIST_TASKS:   self._handle_list_tasks,
            Intent.CREATE_TASK:  self._handle_create_task,
            Intent.UPDATE_TASK:  self._handle_update_task,
            Intent.DELETE_TASK:  self._handle_delete_task,
            Intent.SEARCH_TASKS: self._handle_search_tasks,
        }

        handler = dispatch.get(intent)
        if not handler:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"Unknown tasks intent: {intent!r}",
            )

        return await handler(message, user_id)

    # ------------------------------------------------------------------
    # Intent: list_tasks
    # ------------------------------------------------------------------

    async def _handle_list_tasks(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        query = message.payload.get("query", "").lower()
        show_completed = "completed" in query or "done" in query or "finished" in query
        start = time.time()

        try:
            tasks = await self._tasks.list_tasks(
                user_id=user_id or "",
                show_completed=show_completed,
            )
        except ValueError as exc:
            # No credentials — guide user to connect
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.error(f"📋 TasksAgent.list_tasks failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"list_tasks failed: {exc}",
            )

        result = self._format_task_list(tasks)
        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"📋 TasksAgent.list_tasks: {len(tasks)} tasks in {duration_ms}ms"
        )
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={"task_count": len(tasks), "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: create_task
    # ------------------------------------------------------------------

    async def _handle_create_task(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        query = message.payload.get("query", "")
        account_id = message.context.get("account_id")
        start = time.time()

        # LLM extracts structured params
        params = await self._extract_task_params(
            query=query,
            operation="create",
            user_id=user_id,
            account_id=account_id,
        )

        title = params.get("title") or query
        notes = params.get("notes")
        due_date = self._parse_date(params.get("due_date"))

        try:
            task = await self._tasks.create_task(
                user_id=user_id or "",
                task=TaskCreate(title=title, notes=notes, due_date=due_date),
            )
        except Exception as exc:
            logger.error(f"📋 TasksAgent.create_task failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"create_task failed: {exc}",
            )

        duration_ms = int((time.time() - start) * 1000)
        logger.info(
            f"📋 TasksAgent.create_task: '{title}' in {duration_ms}ms"
        )
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=f"Task created: {task.to_display_string()}",
            confidence=1.0,
            metadata={"task_id": task.task_id, "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: update_task
    # ------------------------------------------------------------------

    async def _handle_update_task(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        query = message.payload.get("query", "")
        task_id = message.payload.get("task_id", "")
        account_id = message.context.get("account_id")
        start = time.time()

        # If no task_id in payload, find task by name via LLM + search
        if not task_id:
            task_id = await self._resolve_task_id(
                query=query, user_id=user_id or "", account_id=account_id
            )
            if not task_id:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="Could not find a matching task. Please be more specific.",
                )

        # LLM extracts what to update
        params = await self._extract_task_params(
            query=query,
            operation="update",
            user_id=user_id,
            account_id=account_id,
        )

        updates = TaskUpdate(
            title=params.get("title"),
            notes=params.get("notes"),
            due_date=self._parse_date(params.get("due_date")),
            status=TaskStatus(params["status"]) if params.get("status") else None,
        )

        try:
            task = await self._tasks.update_task(
                user_id=user_id or "",
                task_id=task_id,
                updates=updates,
            )
        except ValueError as exc:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.error(f"📋 TasksAgent.update_task failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"update_task failed: {exc}",
            )

        duration_ms = int((time.time() - start) * 1000)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=f"Task updated: {task.to_display_string()}",
            confidence=1.0,
            metadata={"task_id": task.task_id, "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: delete_task
    # ------------------------------------------------------------------

    async def _handle_delete_task(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        query = message.payload.get("query", "")
        task_id = message.payload.get("task_id", "")
        account_id = message.context.get("account_id")
        start = time.time()

        if not task_id:
            task_id = await self._resolve_task_id(
                query=query, user_id=user_id or "", account_id=account_id
            )
            if not task_id:
                return AgentResponse.failure(
                    task_id=message.task_id,
                    agent_id=self.agent_id,
                    error="Could not find a matching task to delete.",
                )

        try:
            await self._tasks.delete_task(user_id=user_id or "", task_id=task_id)
        except ValueError as exc:
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=str(exc),
            )
        except Exception as exc:
            logger.error(f"📋 TasksAgent.delete_task failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"delete_task failed: {exc}",
            )

        duration_ms = int((time.time() - start) * 1000)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result="Task deleted.",
            confidence=1.0,
            metadata={"task_id": task_id, "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Intent: search_tasks
    # ------------------------------------------------------------------

    async def _handle_search_tasks(
        self, message: AgentMessage, user_id: Optional[str]
    ) -> AgentResponse:
        query = message.payload.get("query", "")
        start = time.time()

        try:
            tasks = await self._tasks.search_tasks(
                user_id=user_id or "",
                query=query,
            )
        except Exception as exc:
            logger.error(f"📋 TasksAgent.search_tasks failed: {exc}", exc_info=True)
            return AgentResponse.failure(
                task_id=message.task_id,
                agent_id=self.agent_id,
                error=f"search_tasks failed: {exc}",
            )

        result = self._format_task_list(tasks)
        duration_ms = int((time.time() - start) * 1000)
        return AgentResponse.success(
            task_id=message.task_id,
            agent_id=self.agent_id,
            result=result,
            confidence=1.0,
            metadata={"task_count": len(tasks), "duration_ms": duration_ms},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_task_list(tasks: List[Task]) -> str:
        """Format tasks list as JSON string for orchestrator consumption."""
        if not tasks:
            return json.dumps({"tasks": [], "summary": "No tasks found."})
        return json.dumps({
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "notes": t.notes,
                    "due_date": t.due_date.strftime("%Y-%m-%d") if t.due_date else None,
                    "status": t.status.value,
                    "display": t.to_display_string(),
                }
                for t in tasks
            ],
            "summary": f"{len(tasks)} task(s)",
        })

    @staticmethod
    def _parse_date(value: object) -> Optional[Any]:
        """Parse YYYY-MM-DD string from LLM output. Returns None on failure."""
        if not value or not isinstance(value, str):
            return None
        from datetime import datetime
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            logger.warning(f"📋 TasksAgent: could not parse date '{value}', ignoring")
            return None

    async def _extract_task_params(
        self,
        query: str,
        operation: str,  # "create" | "update"
        user_id: Optional[str],
        account_id: Optional[str],
    ) -> dict:
        """
        LLM extracts structured task parameters from natural language.

        For create: returns {"title": str, "notes": str|null, "due_date": "YYYY-MM-DD"|null}
        For update: returns {"title": str|null, "notes": str|null,
                             "due_date": "YYYY-MM-DD"|null, "status": "completed"|null}
        """
        system_prompt = ""
        try:
            system_prompt = await self.prompt_builder.build_for_agent(
                agent_type="tasks",
                user_id=user_id,
                account_id=account_id,
                routing_metadata=None,
                include_biographical=False,
            )
        except Exception as exc:
            logger.warning(f"📋 TasksAgent: build_for_agent failed ({exc})")

        user_text = f'TASK_{operation.upper()}_REQUEST "{query}"'
        messages = [Message(role="user", parts=[MessagePart(text=user_text)])]

        request = LLMRequest(
            model_name=self.model_name,
            system_instruction=system_prompt,
            messages=messages,
            tools=[],
            temperature=self.TEMPERATURE,
            max_tokens=self.MAX_TOKENS,
            disable_safety=True,
            response_mime_type="application/json",
        )

        try:
            response = await self._call_llm(request)
            raw = (response.text or "").strip()
            return json.loads(raw)
        except Exception as exc:
            logger.warning(f"📋 TasksAgent: param extraction failed ({exc}), using defaults")
            return {}

    async def _resolve_task_id(
        self, query: str, user_id: str, account_id: Optional[str]
    ) -> Optional[str]:
        """
        Find a task ID by searching the user's task list and matching by title.
        Used when task_id is not provided in the payload.
        """
        try:
            tasks = await self._tasks.search_tasks(user_id=user_id, query=query)
            if tasks:
                return tasks[0].task_id
        except Exception as exc:
            logger.warning(f"📋 TasksAgent: _resolve_task_id failed ({exc})")
        return None
```

---

### 4.5 `tests/unit/agents/test_tasks_agent.py`

```python
"""Unit tests for TasksAgent."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.tasks_agent import TasksAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.task import Task, TaskCreate, TaskStatus
from src.infrastructure.agent_manifest import Intent
from src.ports.llm_port import AgentExecutionContext, LLMResponse, ProviderCapabilities
from src.ports.tasks_provider_port import TasksProviderPort


@pytest.fixture
def mock_tasks_provider():
    return AsyncMock(spec=TasksProviderPort)


@pytest.fixture
def mock_llm():
    m = AsyncMock()
    m.generate_content = AsyncMock(
        return_value=LLMResponse(
            text=json.dumps({"title": "Buy milk", "notes": None, "due_date": None}),
            tool_calls=[],
            finish_reason="STOP",
        )
    )
    m.get_capabilities = MagicMock(return_value=ProviderCapabilities())
    return m


@pytest.fixture
def mock_prompt_builder():
    m = AsyncMock()
    m.build_for_agent = AsyncMock(return_value="")
    return m


@pytest.fixture
def agent(mock_llm, mock_prompt_builder, mock_tasks_provider):
    config = AgentConfig(
        agent_id="tasks_agent_user123",
        agent_type="tasks",
        timeout_ms=20_000,
        capabilities=["task_management"],
    )
    context = AgentExecutionContext(
        agent_type="tasks",
        provider=mock_llm,
        model_name="gemini-2.0-flash",
        tier=None,
        capabilities=ProviderCapabilities(),
    )
    return TasksAgent(
        config=config,
        execution_context=context,
        prompt_builder=mock_prompt_builder,
        tasks_provider=mock_tasks_provider,
        user_id="user123",
    )


def _make_message(intent: str, query: str, task_id: str = "") -> AgentMessage:
    return AgentMessage(
        task_id="task-1",
        intent=AgentIntent.QUERY,
        payload={"intent": intent, "query": query, "task_id": task_id},
        context={"user_id": "user123", "account_id": "account1"},
    )


class TestTasksAgentCanHandle:
    async def test_can_handle_list_tasks(self, agent):
        msg = _make_message(Intent.LIST_TASKS, "show my tasks")
        assert await agent.can_handle(msg) is True

    async def test_can_handle_create_task(self, agent):
        msg = _make_message(Intent.CREATE_TASK, "create task: buy milk")
        assert await agent.can_handle(msg) is True

    async def test_rejects_unknown_intent(self, agent):
        msg = AgentMessage(
            task_id="task-1",
            intent=AgentIntent.QUERY,
            payload={"intent": "search_emails", "query": "find emails"},
            context={},
        )
        # Can still handle if query is present — intent routing handles correctness
        # The real guard is the dispatcher in execute()


class TestTasksAgentListTasks:
    async def test_execute_list_tasks_returns_json(self, agent, mock_tasks_provider):
        mock_tasks_provider.list_tasks.return_value = [
            Task(task_id="id1", title="Buy milk", status=TaskStatus.NEEDS_ACTION),
            Task(task_id="id2", title="Call dentist", status=TaskStatus.NEEDS_ACTION),
        ]
        msg = _make_message(Intent.LIST_TASKS, "show my tasks")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        data = json.loads(response.result)
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["title"] == "Buy milk"

    async def test_list_tasks_empty_returns_no_tasks_summary(
        self, agent, mock_tasks_provider
    ):
        mock_tasks_provider.list_tasks.return_value = []
        msg = _make_message(Intent.LIST_TASKS, "show tasks")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        data = json.loads(response.result)
        assert data["tasks"] == []

    async def test_list_tasks_no_credentials_returns_failure(
        self, agent, mock_tasks_provider
    ):
        mock_tasks_provider.list_tasks.side_effect = ValueError("No credentials found")
        msg = _make_message(Intent.LIST_TASKS, "show tasks")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILURE
        assert "credentials" in response.error.lower()


class TestTasksAgentCreateTask:
    async def test_execute_create_task_success(
        self, agent, mock_tasks_provider, mock_llm
    ):
        mock_tasks_provider.create_task.return_value = Task(
            task_id="new-id",
            title="Buy milk",
            status=TaskStatus.NEEDS_ACTION,
        )
        msg = _make_message(Intent.CREATE_TASK, "create task: buy milk")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "Buy milk" in response.result
        mock_tasks_provider.create_task.assert_called_once()

    async def test_create_task_provider_error_returns_failure(
        self, agent, mock_tasks_provider
    ):
        mock_tasks_provider.create_task.side_effect = RuntimeError("API error")
        msg = _make_message(Intent.CREATE_TASK, "create task: test")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILURE


class TestTasksAgentSearchTasks:
    async def test_search_returns_matching_tasks(self, agent, mock_tasks_provider):
        mock_tasks_provider.search_tasks.return_value = [
            Task(task_id="id1", title="Buy groceries", status=TaskStatus.NEEDS_ACTION),
        ]
        msg = _make_message(Intent.SEARCH_TASKS, "find tasks about groceries")
        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        data = json.loads(response.result)
        assert len(data["tasks"]) == 1
```

---

### 4.6 `tests/unit/ports/test_tasks_provider_port.py`

```python
"""Contract test: TasksProviderPort defines the correct abstract interface."""

import inspect

import pytest

from src.ports.tasks_provider_port import TasksProviderPort


def test_port_is_abstract():
    assert not inspect.isabstract(TasksProviderPort) is False  # has abstractmethods
    assert len(TasksProviderPort.__abstractmethods__) == 5


def test_port_has_list_tasks():
    assert "list_tasks" in TasksProviderPort.__abstractmethods__


def test_port_has_create_task():
    assert "create_task" in TasksProviderPort.__abstractmethods__


def test_port_has_update_task():
    assert "update_task" in TasksProviderPort.__abstractmethods__


def test_port_has_delete_task():
    assert "delete_task" in TasksProviderPort.__abstractmethods__


def test_port_has_search_tasks():
    assert "search_tasks" in TasksProviderPort.__abstractmethods__
```

---

### 4.7 `firestore_utils/uploads/COGNITIVE_PROCESS_TASKS.groovy`

```groovy
cognitive_process {

    instruction: "Internal process — never output these steps."

    steps: [

        """1. INTENT: What task operation does the user need?
           Classify into exactly one of:
             • list_tasks   — show/display tasks (incl. "what do I need to do",
                              "tasks for today", "overdue tasks", "what's pending")
             • create_task  — add/create/remind (extract title, optional notes, optional due date)
             • update_task  — edit/change/mark done/complete/incomplete (extract what changes)
             • delete_task  — remove/delete/cancel a task
             • search_tasks — find/search/look for tasks by keyword

           Key extraction rules:
             • Title: the core action, cleaned of filler ("create task to", "remind me to", etc.)
             • Due date: convert natural language → YYYY-MM-DD (today, tomorrow, "next Monday")
             • Notes: any detail beyond the title (why, how, what specifically)
             • Status: "completed" for mark-as-done, null otherwise

           Output as JSON:
             create: {"title": "...", "notes": "...|null", "due_date": "YYYY-MM-DD|null"}
             update: {"title": "...|null", "notes": "...|null",
                      "due_date": "YYYY-MM-DD|null", "status": "completed|needsAction|null"}
             other:  {} (empty — no extraction needed)""",

        """2. LIST TASKS — date interpretation rule (CRITICAL):
           list_tasks ALWAYS returns all active tasks (no server-side date filter exists).
           The full list is handed to you. You interpret date-based queries yourself:

             "tasks for today"   → show tasks where due_date == today OR due_date is null OR due_date < today
             "overdue tasks"     → show tasks where due_date < today
             "upcoming tasks"    → show tasks where due_date >= today OR due_date is null
             "tasks this week"   → show tasks where due_date <= end_of_week OR due_date is null

           Never pass a date filter in the payload — the agent ignores it.
           Never tell the user "I filtered by date" — just present the relevant subset naturally.""",

        """3. FORMAT: Confirm the action clearly and briefly.
           • For list/search: present the result as a readable list; apply date interpretation from step 2.
           • For create/update/delete: one-line confirmation ("Task created: ⬜ Buy milk (due 2026-03-10)").
           • Never include the raw JSON in the user-facing response."""
    ]
}
```

---

### 4.8 `firestore_utils/uploads/tasks_agent_v1.json`

This is the **blueprint** for the tasks agent.

```json
{
  "blueprint_id": "tasks_agent_v1",
  "outer_class": "TasksAgent extends Agent",
  "class_order": [
    "cognitive_process",
    "output_format"
  ]
}
```

---

### 4.9 `firestore_utils/uploads/tasks.json`

This is the **agent profile** (token assignments).

```json
{
  "blueprint_id": "tasks_agent_v1",
  "agent_id": "tasks",
  "tokens": {
    "COGNITIVE_PROCESS_TASKS": {
      "order": 10,
      "non_overridable": true
    }
  }
}
```

---

## 5. Implementation: Modified Files

### 5.1 `src/infrastructure/agent_manifest.py`

**Add to `Intent` class** (after `EXECUTE_DEEP_RESEARCH_CLAUDE`):

```python
    # Tasks management intents
    LIST_TASKS   = "list_tasks"
    CREATE_TASK  = "create_task"
    UPDATE_TASK  = "update_task"
    DELETE_TASK  = "delete_task"
    SEARCH_TASKS = "search_tasks"
```

**Add `TASKS` descriptor** (after `DEEP_RESEARCH_AGENT`, before `CLAUDE_DEEP_RESEARCH_RUNNER`):

```python
TASKS = AgentDescriptor(
    agent_id="tasks_agent",
    agent_type="tasks",
    capabilities={
        Intent.LIST_TASKS:   ExecutionMode.SYNC,
        Intent.CREATE_TASK:  ExecutionMode.SYNC,
        Intent.UPDATE_TASK:  ExecutionMode.SYNC,
        Intent.DELETE_TASK:  ExecutionMode.SYNC,
        Intent.SEARCH_TASKS: ExecutionMode.SYNC,
    },
    description="Task management specialist (Google Tasks)",
    capability_descriptions={
        Intent.LIST_TASKS: (
            "List all tasks in the user's task list. "
            "Use when the user asks to see, show, or display their tasks. "
            'payload: {"query": "show my tasks"}'
        ),
        Intent.CREATE_TASK: (
            "Create a new task. Extracts title, optional notes, optional due date. "
            "Use for: 'add task', 'remind me to', 'create a task to', 'todo: ...'. "
            'payload: {"query": "<natural language task description>"}'
        ),
        Intent.UPDATE_TASK: (
            "Update an existing task: change title, notes, due date, or mark as complete/incomplete. "
            "Use for: 'mark as done', 'complete', 'reschedule', 'edit task'. "
            'payload: {"query": "<what to change>", "task_id": "<id if known>"}'
        ),
        Intent.DELETE_TASK: (
            "Delete a task by name or ID. "
            "Use for: 'delete', 'remove', 'cancel' a task. "
            'payload: {"query": "<task name or description>", "task_id": "<id if known>"}'
        ),
        Intent.SEARCH_TASKS: (
            "Search tasks by keyword in title or notes. "
            "Use when user wants to find a specific task. "
            'payload: {"query": "<search term>"}'
        ),
    },
    internal=False,
)
```

**Add to `ALL_DESCRIPTORS`**:

```python
ALL_DESCRIPTORS = [
    MEMORY_SEARCH,
    WEB_SEARCH,
    WEB_SEARCH_LIGHT,
    EMAIL_SEARCH,
    MAPS_SEARCH,
    COMPUTE,
    DEEP_RESEARCH_AGENT,
    TASKS,            # ← add here
]
```

**Why no other changes are needed for Quick/Smart routing:**

`QUICK_RESPONSE` and `SMART_RESPONSE` descriptors both declare `allowed_intents=None`.
`AgentRegistry.get_available_intents_for()` interprets `None` as "all non-internal intents".
Adding TASKS to `ALL_DESCRIPTORS` with `internal=False` is sufficient — Quick and Smart will
automatically include tasks intents in their LLM tool declarations on the next request.
No whitelist, no `DEFAULT_INTENTS` frozenset, no changes to orchestrator agents required.

---

### 5.2 `src/infrastructure/agent_config.py`

**Add after `DeepResearchAgentConfig`**:

```python
# ---------------------------------------------------------------------------
# TasksAgent (src/agents/tasks_agent.py)
# ---------------------------------------------------------------------------

@dataclass
class TasksAgentConfig:
    temperature: float = 0.3      # Deterministic param extraction
    max_tokens: int = 256         # Small: just structured JSON output
    timeout_ms: int = 20_000      # Single LLM call + API call


TASKS = TasksAgentConfig()
```

---

### 5.3 `src/services/agent_context_builder.py`

**Add to `STRATEGIES` dict** (after `"maps_search"` entry):

```python
        "tasks": {
            "default_provider": "gemini",
            "allowed_providers": ["gemini", "claude"],
            "required_capabilities": ["native_tools"],
            "fallback": "gemini"
        },
```

---

### 5.4 `src/composition/service_container.py`

**Add import** (after gmail imports):

```python
from ..adapters.google_tasks_adapter import GoogleTasksAdapter
```

**Add in `__init__`** (after `self.email_search_service` block, around line 91):

```python
        # ------------------------------------------------------------------
        # Tasks management adapter (shared, stateless per worker)
        # ------------------------------------------------------------------
        self.google_tasks_provider = GoogleTasksAdapter(
            oauth_credentials_repo=self.oauth_credentials,
            client_id=config.get("GOOGLE_OAUTH_CLIENT_ID", ""),
            client_secret=config.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        )
```

**Add to `agent_services()`** (after `"indexed_email_repo"`):

```python
            "tasks_provider": self.google_tasks_provider,
```

---

### 5.5 `src/composition/user_agent_factory.py`

**Touch point 1 — Add import** (after `from ..infrastructure.agent_config import ...`):

```python
from ..infrastructure.agent_config import (
    # ... existing imports ...
    TASKS as TASKS_CFG,
)
from ..agents.tasks_agent import TasksAgent
from ..ports.tasks_provider_port import TasksProviderPort
```

**Touch point 2 — Add constructor parameter** (after `task_queue`):

```python
        tasks_provider: Optional[TasksProviderPort] = None,
```

And store it:
```python
        self.tasks_provider = tasks_provider
```

**Touch point 3 — Instantiate in `_create_and_cache_agents`** (after `compute_agent` block):

```python
        tasks_agent = None
        if self.tasks_provider:
            tasks_context = self.context_builder.build("tasks", user_profile.config)
            tasks_agent = TasksAgent(
                config=AgentConfig(
                    agent_id=f"tasks_agent_{user_id}",
                    agent_type="tasks",
                    timeout_ms=TASKS_CFG.timeout_ms,
                    capabilities=["task_management"],
                ),
                execution_context=tasks_context,
                prompt_builder=prompt_builder,
                tasks_provider=self.tasks_provider,
                user_id=user_id,
            )
```

**Touch point 4 — Register**:

```python
        if tasks_agent:
            agents_to_register.append(tasks_agent)
```

**Touch point 5 — Add to cache dict**:

```python
            "tasks_agent": tasks_agent,
```

**Touch point 6 — Add to eviction tuple** (line 515-518 in `_evict_expired_cache`):

```python
                for key in ("router_agent", "quick_agent", "smart_agent",
                            "memory_agent", "web_agent", "web_search_light_agent",
                            "email_search_agent", "maps_agent", "compute_agent",
                            "tasks_agent",                    # ← add here
                            "deep_research_agent", "claude_runner_agent", "consolidation_agent"):
```

---

### 5.6 `src/web/oauth_app.py`

**Add Tasks OAuth service param** — add to function signature:

```python
def create_oauth_blueprint(
    auth_service: AuthenticationService,
    session_service: SessionService,
    auth_registry: AuthProviderRegistry,
    auth_config: AuthConfig,
    invite_service: Optional['InviteCodeService'] = None,
    gmail_oauth_service: Optional[GoogleOAuthService] = None,
    oauth_credentials_port: Optional[OAuthCredentialsPort] = None,
    google_tasks_oauth_service: Optional[GoogleOAuthService] = None,  # ← add
) -> Blueprint:
```

**Note:** See `AuthConfig` — add `google_tasks_oauth_redirect_uri` config field,
or reuse existing one with a different path.

**Add two new endpoints** (at end of blueprint, before `return bp`):

```python
    # ========================================================================
    # GET /auth/connect-google-tasks — Initiate Google Tasks OAuth
    # ========================================================================
    @bp.route("/auth/connect-google-tasks", methods=["GET"])
    async def connect_google_tasks():
        """
        Initiate Google Tasks OAuth (tasks scope).

        Requires: authenticated session (access_token cookie).
        Scope: https://www.googleapis.com/auth/tasks
        """
        if not google_tasks_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Google Tasks integration not configured"}), 501

        access_token = request.cookies.get("access_token")
        if not access_token:
            return redirect("/auth/login?next=/cabinet")

        try:
            user_id = session_service.get_user_from_token(access_token)
        except Exception:
            return redirect("/auth/login?next=/cabinet")

        state = secrets.token_urlsafe(32)
        auth_url = google_tasks_oauth_service.get_authorization_url(
            state=state,
            redirect_uri=auth_config.google_tasks_oauth_redirect_uri,
        )
        logger.info(f"📋 Google Tasks OAuth initiated for user={user_id[:8]}")

        response = await make_response(redirect(auth_url))
        response.set_cookie(
            "tasks_oauth_state", state,
            max_age=600, httponly=True, secure=True, samesite="lax"
        )
        response.set_cookie(
            "tasks_connect_user_id", user_id,
            max_age=600, httponly=True, secure=True, samesite="lax"
        )
        return response

    # ========================================================================
    # GET /auth/connect-google-tasks/callback — Google Tasks OAuth callback
    # ========================================================================
    @bp.route("/auth/connect-google-tasks/callback", methods=["GET"])
    async def connect_google_tasks_callback():
        """Handle Google Tasks OAuth callback: exchange code, persist credentials."""
        if not google_tasks_oauth_service or not oauth_credentials_port:
            return jsonify({"error": "Google Tasks integration not configured"}), 501

        code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")

        if error:
            logger.warning(f"⚠️ Google Tasks OAuth denied: {error}")
            return redirect("/cabinet?tasks_error=denied")

        stored_state = request.cookies.get("tasks_oauth_state")
        user_id = request.cookies.get("tasks_connect_user_id")

        if not stored_state or stored_state != state or not user_id:
            logger.warning("⚠️ Google Tasks OAuth CSRF validation failed")
            return redirect("/cabinet?tasks_error=state")

        if not code:
            return redirect("/cabinet?tasks_error=no_code")

        try:
            credentials = await google_tasks_oauth_service.exchange_code(
                code=code,
                redirect_uri=auth_config.google_tasks_oauth_redirect_uri,
                user_id=user_id,
            )
            # Override provider to distinguish from gmail credentials
            from ..domain.email import OAuthCredentials
            credentials = OAuthCredentials(
                **{**credentials.model_dump(), "provider": "google_tasks"}
            )
            await oauth_credentials_port.save_credentials(credentials)
            logger.info(f"✅ Google Tasks connected for user={user_id[:8]}")
        except Exception as exc:
            logger.error(f"💥 Google Tasks OAuth callback failed: {exc}")
            return redirect("/cabinet?tasks_error=exchange")

        response = await make_response(redirect("/cabinet?tasks_connected=1"))
        response.delete_cookie("tasks_oauth_state")
        response.delete_cookie("tasks_connect_user_id")
        return response
```

**Also needed**: Add `google_tasks_oauth_redirect_uri` to `AuthConfig` and to the
call site in `main.py` where `create_oauth_blueprint()` is invoked.

**`GoogleOAuthService` note**: `GmailOAuthService` is renamed to `GoogleOAuthService`
(file: `gmail_oauth_service.py` → `google_oauth_service.py`) because it is not Gmail-specific —
it handles any Google OAuth flow. Create a second instance in `ServiceContainer`
initialized with Tasks scope:

```python
# In ServiceContainer.__init__, after gmail_oauth_service:
from ..services.google_oauth_service import GoogleOAuthService
self.google_tasks_oauth_service = GoogleOAuthService(
    client_id=config.get("GOOGLE_OAUTH_CLIENT_ID", ""),
    client_secret=config.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    scopes=["https://www.googleapis.com/auth/tasks"],
)
```

---

## 6. Firestore Upload Commands

Run in order (each upload must succeed before the next):

```bash
# 1. Upload cognitive process token
python firestore_utils/upload.py \
  firestore_utils/uploads/COGNITIVE_PROCESS_TASKS.groovy \
  --collection development_domain_prompt_tokens_v3_system

# 2. Upload blueprint
python firestore_utils/upload.py \
  firestore_utils/uploads/tasks_agent_v1.json \
  --collection development_domain_prompt_blueprints_v3 \
  --format json

# 3. Upload agent profile
python firestore_utils/upload.py \
  firestore_utils/uploads/tasks.json \
  --collection development_domain_prompt_profiles_v3 \
  --format json
```

**After deploying**, manually update Protocol tokens in Firestore
(`PROTOCOL_SMART_AGENT_SELECTION` and `PROTOCOL_QUICK_AGENT_SELECTION`):

Add to each:
```
tasks_agent rules:
  use list_tasks when: user asks to see/show/display tasks
  use create_task when: user says "add", "create", "remind me to", "todo"
  use update_task when: user says "mark done", "complete", "reschedule", "edit"
  use delete_task when: user says "delete", "remove", "cancel" a specific task
  use search_tasks when: user wants to find a specific task by keyword

  ANTI-PATTERNS:
    - Do NOT use list_tasks for calendar events (use compute_datetime or web_search)
    - Do NOT use create_task for reminders about people/meetings (use memory)
    - Always pass task_id in payload when you have it from a prior list_tasks result
```

---

## 7. Environment Variables

Add to `.env` and GCP Secret Manager:

```
# Already exists for Gmail — reused for Tasks:
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...

# New:
GOOGLE_TASKS_OAUTH_REDIRECT_URI=https://{service-url}/auth/connect-google-tasks/callback
```

OAuth app in GCP Console: add `tasks` scope to the existing OAuth2 client
(`https://www.googleapis.com/auth/tasks`).

---

## 8. AuthConfig Changes

In `src/config/auth.py` (or wherever `AuthConfig` lives):

```python
# Add field:
google_tasks_oauth_redirect_uri: str = ""
```

In `src/config/environment.py` or `main.py`, load from env:
```python
google_tasks_oauth_redirect_uri=os.getenv("GOOGLE_TASKS_OAUTH_REDIRECT_URI", "")
```

---

## 9. Execution Order (Implementation Session)

Execute in this EXACT order. Do not skip steps.

```
Step 1.  Create docs/10_rfcs/TASKS_AGENT_RFC.md (copy this file)
Step 1a. Rename src/services/gmail_oauth_service.py → src/services/google_oauth_service.py;
         rename class GmailOAuthService → GoogleOAuthService inside the file;
         update all import sites (service_container.py, oauth_app.py, main.py)
Step 2.  Create src/domain/task.py
Step 3.  Create src/ports/tasks_provider_port.py
Step 4.  Create src/adapters/google_tasks_adapter.py
Step 5.  Modify src/infrastructure/agent_manifest.py (+Intents, +TASKS descriptor, +ALL_DESCRIPTORS)
Step 6.  Modify src/infrastructure/agent_config.py (+TasksAgentConfig + TASKS)
Step 7.  Modify src/services/agent_context_builder.py (+tasks STRATEGIES entry)
Step 8.  Create src/agents/tasks_agent.py
Step 9.  Modify src/composition/service_container.py (+GoogleTasksAdapter)
Step 10. Modify src/composition/user_agent_factory.py (+4 touch points)
Step 11. Modify src/web/oauth_app.py (+2 endpoints, +signature param)
Step 12. Modify src/config/auth.py (+google_tasks_oauth_redirect_uri)
Step 13. Modify main.py (+google_tasks_oauth_service instantiation + pass to blueprint)
Step 14. Create tests/unit/agents/test_tasks_agent.py
Step 15. Create tests/unit/ports/test_tasks_provider_port.py
Step 16. make test-unit  (must pass)
Step 17. Create firestore_utils/uploads/COGNITIVE_PROCESS_TASKS.groovy
Step 18. Create firestore_utils/uploads/tasks_agent_v1.json
Step 19. Create firestore_utils/uploads/tasks.json
Step 20. Upload to Firestore (see Section 6)
Step 21. make deploy-dev
Step 22. Manual smoke test (see Section 10)
```

---

## 10. Verification

### Unit tests
```bash
make test-unit
# Expected: all existing tests pass + new TasksAgent tests pass
```

### E2E delegation test
```bash
make test-e2e-all
# Verify: Smart/Quick routes "create task" → tasks_agent intent
```

### Manual OAuth flow
```
1. Open /cabinet → click "Connect Google Tasks"
2. Google consent screen → approve tasks scope
3. Callback redirects to /cabinet?tasks_connected=1
4. Verify: OAuthCredentials doc created in Firestore with provider="google_tasks"
```

### Manual Slack smoke tests (in dev environment)

| Message | Expected behavior |
|---------|------------------|
| `show my tasks` | List agent called → JSON returned → orchestrator renders as mrkdwn list |
| `create task: buy milk tomorrow` | Task created in Google Tasks → confirmation in Slack |
| `mark buy milk as done` | Task found via search → status=completed |
| `delete buy milk` | Task deleted |
| `find tasks about milk` | search_tasks called → result rendered |
| `show completed tasks` | list_tasks(show_completed=True) |

---

## 11. Future: Things 3 Adapter Stub

When implementing `Things3Adapter` later:
- Implement `TasksProviderPort` with 5 methods
- Things 3 URL scheme: `things:///add?title=X&notes=Y&when=YYYY-MM-DD`
- Things JSON HTTP API (Mac only): `GET http://localhost:PORT/tasks?token=TOKEN`
- Constructor: `(api_token: str)` — no OAuth, no credentials repo needed
- Add `"things3"` to `STRATEGIES` in `agent_context_builder.py`
- ServiceContainer: `self.things3_provider = Things3Adapter(token=config.get("THINGS3_API_TOKEN", ""))`
- New endpoint: `/auth/connect-things3` (just stores the API token, no OAuth dance)
- Port interface: **no changes needed** — that's the point of the abstraction

---

## 12. Known Limitations

1. **search_tasks is client-side** — Google Tasks has no server-side full-text search.
   For large task lists, this may be slow. Acceptable for personal use (< 100 tasks).

2. **Tasklist cache is in-memory** — resets on worker restart. On next request, adapter
   will re-fetch from Google API (one extra call). Not a problem.

3. **Things 3 adapter is NOT yet implemented** — port design supports it, implementation
   is a separate RFC/task.

4. **No Cabinet UI** — users must connect via `/auth/connect-google-tasks` URL directly.
   Cabinet UI is a separate Milestone 4 item.
