"""
GoogleTasksAdapter — implements TasksProviderPort using Google Tasks REST API.

Auth strategy:
  - Fetches OAuthCredentials from OAuthCredentialsPort by user_id
    (provider="google_tasks")
  - Refreshes access_token when expired (same pattern as GmailProviderAdapter)

Dedicated list:
  - Each user has one list named "Alek Bot Tasks"
  - List ID is cached in memory per instance (worker-level cache)
  - List is created on first use

Google Tasks API base: https://tasks.googleapis.com/tasks/v1/
OAuth scope required: https://www.googleapis.com/auth/tasks
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp

from ..domain.task import Task, TaskCreate, TaskStatus, TaskUpdate
from ..ports.tasks_provider_port import TasksProviderPort
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..domain.email import OAuthCredentials
from ..utils.logger import logger

_TASKS_BASE = "https://tasks.googleapis.com/tasks/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_TASKLIST_NAME = "Alek Bot Tasks"
_PROVIDER = "google_tasks"


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
                logger.debug("Could not parse due date %r for task %s", item.get("due"), item.get("id"))

        updated_at: Optional[datetime] = None
        if item.get("updated"):
            try:
                updated_at = datetime.strptime(
                    item["updated"][:19], "%Y-%m-%dT%H:%M:%S"
                )
            except (ValueError, TypeError):
                logger.debug("Could not parse updated_at %r for task %s", item.get("updated"), item.get("id"))

        return Task(
            task_id=item["id"],
            title=item.get("title", ""),
            notes=item.get("notes"),
            due_date=due_date,
            status=status,
            created_at=updated_at,
            updated_at=updated_at,
            provider=_PROVIDER,
        )
