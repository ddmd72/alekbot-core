"""
MicrosoftToDoAdapter — implements TasksProviderPort + TaskLifecyclePort via Graph API.

Auth: OAuth via OAuthCredentialsPort (provider="microsoft_todo").
Token refresh endpoint: https://login.microsoftonline.com/consumers/oauth2/v2.0/token
Graph API base: https://graph.microsoft.com/v1.0/me/todo/

Design decisions (per RFC §6.1):
- batch_get_tasks: individual GET per ref, semaphore=5, 429 retry with backoff.
- list_tasks(list_id=None): parallel per-list fetches (Graph has no cross-list endpoint).
- checklist diff on update: PATCH changed, DELETE removed, POST new (by item_id).
- ensure_primary_list: per-instance in-memory cache populated from task_config on first call.
- Subscription config persistence is the CALLER'S responsibility (TaskSetupService).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from ..domain.email import OAuthCredentials
from ..domain.task import (
    ChecklistItem,
    LinkedResource,
    RecurrencePattern,
    RecurrenceRange,
    Task,
    TaskCreate,
    TaskImportance,
    TaskList,
    TaskRecurrence,
    TaskStatus,
    TaskSubscriptionConfig,
    TaskUpdate,
)
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_config_port import TaskConfigPort
from ..ports.task_lifecycle_port import SubscriptionNotFoundError, TaskLifecyclePort
from ..ports.tasks_provider_port import TasksProviderPort
from ..utils.logger import logger

_GRAPH_BASE = "https://graph.microsoft.com/v1.0/me/todo"
_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_PROVIDER = "microsoft_todo"
_PRIMARY_LIST_NAME = "Alek Bot Tasks"
_BATCH_SEMAPHORE = asyncio.Semaphore(5)  # Graph API throttling for personal accounts

# Graph API limit for personal (consumers) accounts varies; use a conservative
# 4000 minutes (~2.8 days) to stay safely within any documented ceiling.
_SUB_EXPIRY_MINUTES = 4000

_IMPORTANCE_TO_MS = {
    TaskImportance.LOW: "low",
    TaskImportance.NORMAL: "normal",
    TaskImportance.HIGH: "high",
}
_IMPORTANCE_FROM_MS = {v: k for k, v in _IMPORTANCE_TO_MS.items()}

_STATUS_TO_MS = {
    TaskStatus.NOT_STARTED: "notStarted",
    TaskStatus.IN_PROGRESS: "inProgress",
    TaskStatus.WAITING_ON_OTHERS: "waitingOnOthers",
    TaskStatus.DEFERRED: "deferred",
    TaskStatus.COMPLETED: "completed",
}
_STATUS_FROM_MS = {v: k for k, v in _STATUS_TO_MS.items()}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse MS datetime string 'YYYY-MM-DDTHH:MM:SS.0000000' → naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z").split(".")[0])
    except (ValueError, AttributeError):
        return None


def _dt_payload(dt: Optional[datetime]) -> Optional[Dict[str, str]]:
    """Convert datetime → MS dateTimeTimeZone payload."""
    if dt is None:
        return None
    return {"dateTime": dt.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"}


class MicrosoftToDoAdapter(TasksProviderPort, TaskLifecyclePort):
    """
    Implements TasksProviderPort and TaskLifecyclePort via MS Graph API.
    OAuthCredentials are fetched and refreshed internally by user_id.
    """

    def __init__(
        self,
        oauth_credentials: OAuthCredentialsPort,
        task_config: TaskConfigPort,
        client_id: str,
        client_secret: str,
        webhook_secret: Optional[str] = None,
    ) -> None:
        self._oauth = oauth_credentials
        self._task_config = task_config
        self._client_id = client_id
        self._client_secret = client_secret
        self._webhook_secret = webhook_secret
        # Per-instance in-memory primary_list_id cache: {user_id: list_id}
        self._primary_list_cache: Dict[str, str] = {}
        logger.info("✅ MicrosoftToDoAdapter initialized")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _get_headers(self, user_id: str) -> Dict[str, str]:
        """Return Authorization headers with a valid (refreshed if needed) access token."""
        creds = await self._oauth.get_credentials(user_id, _PROVIDER)
        if creds is None:
            raise ValueError(f"No MS To Do credentials for user {user_id[:8]}")

        # Refresh if token expires within the next 5 minutes
        now = datetime.now(timezone.utc)
        if creds.token_expiry <= now + timedelta(minutes=5):
            creds = await self._refresh_token(creds)

        return {"Authorization": f"Bearer {creds.access_token}"}

    async def _refresh_token(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange refresh_token → new access_token and persist."""
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": "Tasks.ReadWrite offline_access",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ValueError(f"MS token refresh failed ({resp.status}): {body}")
                payload = await resp.json()

        expires_in = payload.get("expires_in", 3600)
        new_creds = OAuthCredentials(
            user_id=creds.user_id,
            provider=_PROVIDER,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", creds.refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=creds.scopes,
            email_address=creds.email_address,
        )
        await self._oauth.save_credentials(new_creds)
        logger.info(f"🔄 MS To Do token refreshed for user {creds.user_id[:8]}")
        return new_creds

    # ------------------------------------------------------------------
    # Graph API helpers
    # ------------------------------------------------------------------

    async def _get(self, user_id: str, path: str) -> Dict[str, Any]:
        headers = await self._get_headers(user_id)
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://graph.microsoft.com/v1.0{path}", headers=headers) as resp:
                if resp.status == 404:
                    raise ValueError(f"Not found: {path}")
                if not resp.ok:
                    body = await resp.text()
                    raise ValueError(f"Graph GET {path} failed ({resp.status}): {body}")
                return await resp.json()

    async def _post(self, user_id: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        headers = {**(await self._get_headers(user_id)), "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://graph.microsoft.com/v1.0{path}", headers=headers, json=body
            ) as resp:
                if not resp.ok:
                    text = await resp.text()
                    raise ValueError(f"Graph POST {path} failed ({resp.status}): {text}")
                return await resp.json()

    async def _patch(self, user_id: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        headers = {**(await self._get_headers(user_id)), "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"https://graph.microsoft.com/v1.0{path}", headers=headers, json=body
            ) as resp:
                if not resp.ok:
                    text = await resp.text()
                    raise ValueError(f"Graph PATCH {path} failed ({resp.status}): {text}")
                return await resp.json()

    async def _delete(self, user_id: str, path: str) -> None:
        headers = await self._get_headers(user_id)
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"https://graph.microsoft.com/v1.0{path}", headers=headers
            ) as resp:
                if resp.status == 404:
                    return  # Already gone — idempotent
                if not resp.ok:
                    text = await resp.text()
                    raise ValueError(f"Graph DELETE {path} failed ({resp.status}): {text}")

    # ------------------------------------------------------------------
    # Domain mapping helpers
    # ------------------------------------------------------------------

    def _task_from_ms(self, data: Dict[str, Any], list_id: str, list_name: str, user_id: str) -> Task:
        """Map Graph API task object → domain Task."""
        importance = _IMPORTANCE_FROM_MS.get(data.get("importance", "normal"), TaskImportance.NORMAL)
        status = _STATUS_FROM_MS.get(data.get("status", "notStarted"), TaskStatus.NOT_STARTED)

        checklist = [
            ChecklistItem(
                item_id=c["id"],
                title=c["displayName"],
                is_completed=c.get("isChecked", False),
                created_at=_parse_dt(c.get("createdDateTime")),
                checked_at=_parse_dt(c.get("checkedDateTime", {}).get("dateTime") if isinstance(c.get("checkedDateTime"), dict) else c.get("checkedDateTime")),
            )
            for c in data.get("checklistItems", [])
        ]

        linked = [
            LinkedResource(
                resource_id=r["id"],
                web_url=r.get("webUrl"),
                display_name=r.get("displayName"),
                application_name=r.get("applicationName"),
                external_id=r.get("externalId"),
            )
            for r in data.get("linkedResources", [])
        ]

        recurrence: Optional[TaskRecurrence] = None
        if data.get("recurrence"):
            rec = data["recurrence"]
            pat = rec.get("pattern", {})
            rng = rec.get("range", {})
            recurrence = TaskRecurrence(
                pattern=RecurrencePattern(
                    type=pat.get("type", "daily"),
                    interval=pat.get("interval", 1),
                    day_of_month=pat.get("dayOfMonth"),
                    days_of_week=pat.get("daysOfWeek", []),
                    first_day_of_week=pat.get("firstDayOfWeek", "sunday"),
                    month=pat.get("month"),
                    index=pat.get("index"),
                ),
                range=RecurrenceRange(
                    type=rng.get("type", "noEnd"),
                    start_date=rng.get("startDate", ""),
                    end_date=rng.get("endDate"),
                    number_of_occurrences=rng.get("numberOfOccurrences"),
                    recurrence_time_zone=rng.get("recurrenceTimeZone"),
                ),
            )

        due = data.get("dueDateTime")
        start = data.get("startDateTime")
        reminder = data.get("reminderDateTime")
        completed = data.get("completedDateTime")

        return Task(
            task_id=data["id"],
            list_id=list_id,
            list_name=list_name,
            user_id=user_id,
            title=data.get("title", ""),
            body=data.get("body", {}).get("content") or None,
            due_datetime=_parse_dt(due.get("dateTime") if isinstance(due, dict) else due),
            start_datetime=_parse_dt(start.get("dateTime") if isinstance(start, dict) else start),
            reminder_datetime=_parse_dt(reminder.get("dateTime") if isinstance(reminder, dict) else reminder),
            is_reminder_on=data.get("isReminderOn", False),
            completed_at=_parse_dt(completed.get("dateTime") if isinstance(completed, dict) else completed),
            importance=importance,
            status=status,
            tags=data.get("categories", []),
            recurrence=recurrence,
            checklist_items=checklist,
            linked_resources=linked,
            created_at=_parse_dt(data.get("createdDateTime")),
            updated_at=_parse_dt(data.get("lastModifiedDateTime")),
        )

    def _task_create_payload(self, task: TaskCreate) -> Dict[str, Any]:
        """Build Graph API create-task request body from TaskCreate."""
        payload: Dict[str, Any] = {
            "title": task.title,
            "importance": _IMPORTANCE_TO_MS.get(task.importance, "normal"),
            "categories": task.tags,
            "isReminderOn": task.is_reminder_on,
        }
        if task.body:
            payload["body"] = {"content": task.body, "contentType": "text"}
        if task.due_datetime:
            payload["dueDateTime"] = _dt_payload(task.due_datetime)
        if task.start_datetime:
            payload["startDateTime"] = _dt_payload(task.start_datetime)
        if task.reminder_datetime:
            payload["reminderDateTime"] = _dt_payload(task.reminder_datetime)
        if task.recurrence:
            payload["recurrence"] = self._recurrence_payload(task.recurrence)
        if task.checklist_items:
            payload["checklistItems"] = [
                {"displayName": c.title, "isChecked": c.is_completed}
                for c in task.checklist_items
            ]
        if task.linked_resources:
            payload["linkedResources"] = [
                {
                    "webUrl": r.web_url,
                    "displayName": r.display_name,
                    "applicationName": r.application_name,
                    "externalId": r.external_id,
                }
                for r in task.linked_resources
            ]
        return payload

    def _task_update_payload(self, updates: TaskUpdate) -> Dict[str, Any]:
        """Build Graph API PATCH body from TaskUpdate (only set fields)."""
        payload: Dict[str, Any] = {}
        if updates.title is not None:
            payload["title"] = updates.title
        if updates.body is not None:
            payload["body"] = {"content": updates.body, "contentType": "text"}
        if updates.importance is not None:
            payload["importance"] = _IMPORTANCE_TO_MS[updates.importance]
        if updates.status is not None:
            payload["status"] = _STATUS_TO_MS[updates.status]
        if updates.tags is not None:
            payload["categories"] = updates.tags
        if updates.is_reminder_on is not None:
            payload["isReminderOn"] = updates.is_reminder_on
        if updates.due_datetime is not None:
            payload["dueDateTime"] = _dt_payload(updates.due_datetime)
        if updates.start_datetime is not None:
            payload["startDateTime"] = _dt_payload(updates.start_datetime)
        if updates.reminder_datetime is not None:
            payload["reminderDateTime"] = _dt_payload(updates.reminder_datetime)
        if updates.recurrence is not None:
            payload["recurrence"] = self._recurrence_payload(updates.recurrence)
        return payload

    @staticmethod
    def _recurrence_payload(rec: TaskRecurrence) -> Dict[str, Any]:
        pat = {
            "type": rec.pattern.type,
            "interval": rec.pattern.interval,
            "firstDayOfWeek": rec.pattern.first_day_of_week,
        }
        if rec.pattern.day_of_month is not None:
            pat["dayOfMonth"] = rec.pattern.day_of_month
        if rec.pattern.days_of_week:
            pat["daysOfWeek"] = rec.pattern.days_of_week
        if rec.pattern.month is not None:
            pat["month"] = rec.pattern.month
        if rec.pattern.index is not None:
            pat["index"] = rec.pattern.index

        rng: Dict[str, Any] = {
            "type": rec.range.type,
            "startDate": rec.range.start_date,
        }
        if rec.range.end_date:
            rng["endDate"] = rec.range.end_date
        if rec.range.number_of_occurrences:
            rng["numberOfOccurrences"] = rec.range.number_of_occurrences
        if rec.range.recurrence_time_zone:
            rng["recurrenceTimeZone"] = rec.range.recurrence_time_zone

        return {"pattern": pat, "range": rng}

    # ------------------------------------------------------------------
    # TasksProviderPort — list_task_lists
    # ------------------------------------------------------------------

    async def list_task_lists(self, user_id: str) -> List[TaskList]:
        data = await self._get(user_id, "/me/todo/lists")
        return [
            TaskList(
                list_id=lst["id"],
                name=lst["displayName"],
                is_owner=lst.get("isOwner", True),
                is_shared=lst.get("isShared", False),
            )
            for lst in data.get("value", [])
        ]

    # ------------------------------------------------------------------
    # TasksProviderPort — list_tasks
    # ------------------------------------------------------------------

    async def list_tasks(
        self,
        user_id: str,
        list_id: Optional[str] = None,
        show_completed: bool = False,
    ) -> List[Task]:
        if list_id is None:
            list_id = await self._resolve_primary_list_id(user_id)
        return await self._fetch_tasks_for_list(user_id, list_id, show_completed)

    async def _fetch_tasks_for_list(
        self,
        user_id: str,
        list_id: str,
        show_completed: bool,
        list_name: Optional[str] = None,
    ) -> List[Task]:
        """Fetch tasks for a single list with pagination."""
        if list_name is None:
            # Resolve list name if not provided
            try:
                lst_data = await self._get(user_id, f"/me/todo/lists/{list_id}")
                list_name = lst_data.get("displayName", "")
            except Exception:
                list_name = ""

        url = f"/me/todo/lists/{list_id}/tasks"
        params: Dict[str, str] = {}
        if not show_completed:
            params["$filter"] = "status ne 'completed'"

        tasks: List[Task] = []
        next_link: Optional[str] = None

        while True:
            if next_link:
                # next_link is a full URL from Graph API
                headers = await self._get_headers(user_id)
                async with aiohttp.ClientSession() as session:
                    async with session.get(next_link, headers=headers) as resp:
                        if not resp.ok:
                            break
                        data = await resp.json()
            else:
                path = url + (f"?{self._build_params(params)}" if params else "")
                data = await self._get(user_id, path)

            for item in data.get("value", []):
                tasks.append(self._task_from_ms(item, list_id, list_name, user_id))

            next_link = data.get("@odata.nextLink")
            if not next_link:
                break

        return tasks

    @staticmethod
    def _build_params(params: Dict[str, str]) -> str:
        from urllib.parse import urlencode
        return urlencode(params)

    # ------------------------------------------------------------------
    # TasksProviderPort — get_task
    # ------------------------------------------------------------------

    async def get_task(self, user_id: str, list_id: str, task_id: str) -> Task:
        data = await self._get(user_id, f"/me/todo/lists/{list_id}/tasks/{task_id}")
        # Resolve list name
        try:
            lst_data = await self._get(user_id, f"/me/todo/lists/{list_id}")
            list_name = lst_data.get("displayName", "")
        except Exception:
            list_name = ""
        return self._task_from_ms(data, list_id, list_name, user_id)

    # ------------------------------------------------------------------
    # TasksProviderPort — batch_get_tasks
    # ------------------------------------------------------------------

    async def batch_get_tasks(
        self, user_id: str, task_refs: List[Tuple[str, str]]
    ) -> List[Task]:
        """Fetch multiple tasks. Bounded concurrency=5. 429 → backoff + retry (max 3)."""
        if not task_refs:
            return []

        async def _fetch_one(list_id: str, task_id: str) -> Optional[Task]:
            for attempt in range(3):
                async with _BATCH_SEMAPHORE:
                    try:
                        return await self.get_task(user_id, list_id, task_id)
                    except ValueError as e:
                        if "429" in str(e):
                            wait = 2 ** attempt
                            logger.warning(f"⏳ MS 429 on task {task_id[:8]}, retry in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"⚠️ MS: task {task_id[:8]} fetch failed: {e}")
                        return None
            return None

        results = await asyncio.gather(*[_fetch_one(lid, tid) for lid, tid in task_refs])
        return [t for t in results if t is not None]

    # ------------------------------------------------------------------
    # TasksProviderPort — create_task
    # ------------------------------------------------------------------

    async def create_task(self, user_id: str, task: TaskCreate) -> Task:
        list_id = task.list_id or await self._resolve_primary_list_id(user_id)
        payload = self._task_create_payload(task)

        data = await self._post(user_id, f"/me/todo/lists/{list_id}/tasks", payload)

        # Resolve list name for Task object
        try:
            lst_data = await self._get(user_id, f"/me/todo/lists/{list_id}")
            list_name = lst_data.get("displayName", "")
        except Exception:
            list_name = ""

        logger.info(f"✅ MS: created task '{task.title[:40]}' in list {list_id[:8]}")
        return self._task_from_ms(data, list_id, list_name, user_id)

    # ------------------------------------------------------------------
    # TasksProviderPort — update_task
    # ------------------------------------------------------------------

    async def update_task(
        self, user_id: str, list_id: str, task_id: str, updates: TaskUpdate
    ) -> Task:
        # Main task PATCH (fields other than checklist/linked resources)
        payload = self._task_update_payload(updates)
        if payload:
            await self._patch(user_id, f"/me/todo/lists/{list_id}/tasks/{task_id}", payload)

        # Checklist diff: PATCH changed, DELETE removed, POST new
        if updates.checklist_items is not None:
            await self._sync_checklist(user_id, list_id, task_id, updates.checklist_items)

        return await self.get_task(user_id, list_id, task_id)

    async def _sync_checklist(
        self,
        user_id: str,
        list_id: str,
        task_id: str,
        desired: List[ChecklistItem],
    ) -> None:
        """
        Diff checklist against existing items. PATCH changed, DELETE removed, POST new.
        Full array replace must NOT be used — it destroys checked_at timestamps.
        """
        existing_data = await self._get(
            user_id, f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems"
        )
        existing: Dict[str, Dict] = {
            c["id"]: c for c in existing_data.get("value", [])
        }
        desired_ids = {c.item_id for c in desired if c.item_id}

        # DELETE items that are no longer present
        for item_id in list(existing.keys()):
            if item_id not in desired_ids:
                await self._delete(
                    user_id,
                    f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems/{item_id}",
                )

        for item in desired:
            if item.item_id and item.item_id in existing:
                # PATCH existing if changed
                ex = existing[item.item_id]
                if ex.get("displayName") != item.title or ex.get("isChecked") != item.is_completed:
                    await self._patch(
                        user_id,
                        f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems/{item.item_id}",
                        {"displayName": item.title, "isChecked": item.is_completed},
                    )
            else:
                # POST new item
                await self._post(
                    user_id,
                    f"/me/todo/lists/{list_id}/tasks/{task_id}/checklistItems",
                    {"displayName": item.title, "isChecked": item.is_completed},
                )

    # ------------------------------------------------------------------
    # TasksProviderPort — delete_task
    # ------------------------------------------------------------------

    async def delete_task(self, user_id: str, list_id: str, task_id: str) -> None:
        await self._delete(user_id, f"/me/todo/lists/{list_id}/tasks/{task_id}")
        logger.info(f"🗑️ MS: deleted task {task_id[:8]} from list {list_id[:8]}")

    # ------------------------------------------------------------------
    # TaskLifecyclePort — ensure_primary_list
    # ------------------------------------------------------------------

    async def ensure_primary_list(self, user_id: str) -> str:
        """
        Find "Alek Bot Tasks" list, create if absent.
        Per-instance in-memory cache for hot-path.
        Does NOT persist — caller (TaskSetupService) persists via TaskConfigPort.
        """
        # Check in-memory cache first
        if user_id in self._primary_list_cache:
            return self._primary_list_cache[user_id]

        # Try loading from TaskConfigPort
        config = await self._task_config.get_config(user_id)
        if config.primary_list_id:
            self._primary_list_cache[user_id] = config.primary_list_id
            return config.primary_list_id

        # Resolve from Graph API
        lists = await self.list_task_lists(user_id)
        for lst in lists:
            if lst.name == _PRIMARY_LIST_NAME:
                self._primary_list_cache[user_id] = lst.list_id
                return lst.list_id

        # Create the list
        data = await self._post(user_id, "/me/todo/lists", {"displayName": _PRIMARY_LIST_NAME})
        list_id = data["id"]
        self._primary_list_cache[user_id] = list_id
        logger.info(f"✅ MS: created primary list '{_PRIMARY_LIST_NAME}' for user {user_id[:8]}")
        return list_id

    async def _resolve_primary_list_id(self, user_id: str) -> str:
        """Used internally by create_task when list_id is not specified."""
        return await self.ensure_primary_list(user_id)

    # ------------------------------------------------------------------
    # TaskLifecyclePort — subscriptions
    # ------------------------------------------------------------------

    async def register_subscription(
        self, user_id: str, list_id: str, notification_url_base: str
    ) -> TaskSubscriptionConfig:
        """
        POST /subscriptions for the given list.
        user_id embedded in webhook URL path → O(1) routing.
        Returns config. Does NOT persist — caller persists.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=_SUB_EXPIRY_MINUTES)
        payload = {
            "changeType": "created,updated,deleted",
            "notificationUrl": f"{notification_url_base}/webhook/microsoft-tasks/{user_id}",
            "resource": f"/me/todo/lists/{list_id}/tasks",
            "expirationDateTime": expires_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        }
        if self._webhook_secret:
            payload["clientState"] = self._webhook_secret
        data = await self._post(user_id, "/subscriptions", payload)
        sub_id = data["id"]
        logger.info(f"📡 MS: registered subscription {sub_id[:8]} for list {list_id[:8]}")
        return TaskSubscriptionConfig(sub_id=sub_id, list_id=list_id, expires_at=expires_at)

    async def renew_subscription(
        self, user_id: str, sub_id: str
    ) -> TaskSubscriptionConfig:
        """
        PATCH subscription with new expiry. Does NOT persist.

        Raises SubscriptionNotFoundError if Graph returns 404 —
        the subscription was hard-deleted past provider retention and
        must be replaced via register_subscription.
        """
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=_SUB_EXPIRY_MINUTES)
        path = f"/subscriptions/{sub_id}"
        body = {"expirationDateTime": expires_at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")}
        headers = {**(await self._get_headers(user_id)), "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"https://graph.microsoft.com/v1.0{path}", headers=headers, json=body
            ) as resp:
                if resp.status == 404:
                    raise SubscriptionNotFoundError(sub_id)
                if not resp.ok:
                    text = await resp.text()
                    raise ValueError(f"Graph PATCH {path} failed ({resp.status}): {text}")
                # Graph PATCH on a subscription usually returns 200 + the updated
                # resource, but can return 204 No Content — a successful renewal
                # with no body. A 204 (or any empty body) has no JSON to decode;
                # treat it as success and leave list_id empty. The caller keeps the
                # previously stored list_id when this comes back blank.
                if resp.status == 204:
                    data = {}
                else:
                    try:
                        data = await resp.json()
                    except aiohttp.ContentTypeError:
                        data = {}
        list_id = ""
        resource = data.get("resource", "")
        if "/lists/" in resource and "/tasks" in resource:
            list_id = resource.split("/lists/")[1].split("/tasks")[0]
        return TaskSubscriptionConfig(sub_id=sub_id, list_id=list_id, expires_at=expires_at)

    async def delete_subscription(self, user_id: str, sub_id: str) -> None:
        await self._delete(user_id, f"/subscriptions/{sub_id}")
        logger.info(f"🗑️ MS: deleted subscription {sub_id[:8]}")

