"""
Task domain models — Microsoft To Do integration.

Task (full model) lives in MS To Do; not stored in Firestore.
TaskSearchEntry is stored in Firestore as a thin search index.
TaskUserConfig is per-user integration config stored in Firestore.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """Mirrors MS To Do taskStatus enum directly."""

    NOT_STARTED = "notStarted"
    IN_PROGRESS = "inProgress"
    WAITING_ON_OTHERS = "waitingOnOthers"
    DEFERRED = "deferred"
    COMPLETED = "completed"


class TaskImportance(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class ChecklistItem:
    """Mirrors MS To Do checklistItem."""

    item_id: str
    title: str
    is_completed: bool = False
    created_at: Optional[datetime] = None
    checked_at: Optional[datetime] = None


@dataclass
class TaskAttachment:
    """
    File attached to a task.
    - Uploaded via bot: stored in GCS, pushed to MS To Do as base64 (max 3 MB per MS limit)
    - External link: url field only
    """

    attachment_id: str
    filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    gcs_uri: Optional[str] = None
    url: Optional[str] = None


@dataclass
class LinkedResource:
    """Mirrors MS To Do linkedResource."""

    resource_id: str
    web_url: Optional[str] = None
    display_name: Optional[str] = None
    application_name: Optional[str] = None
    external_id: Optional[str] = None


@dataclass
class RecurrencePattern:
    """
    Mirrors MS To Do recurrencePattern.
    type: "daily" | "weekly" | "absoluteMonthly" | "relativeMonthly" |
          "absoluteYearly" | "relativeYearly"
    """

    type: str
    interval: int = 1
    day_of_month: Optional[int] = None
    days_of_week: List[str] = field(default_factory=list)
    first_day_of_week: str = "sunday"
    month: Optional[int] = None
    index: Optional[str] = None


@dataclass
class RecurrenceRange:
    """
    Mirrors MS To Do recurrenceRange.
    type: "endDate" | "noEnd" | "numbered"
    """

    type: str
    start_date: str  # YYYY-MM-DD
    end_date: Optional[str] = None
    number_of_occurrences: Optional[int] = None
    recurrence_time_zone: Optional[str] = None


@dataclass
class TaskRecurrence:
    pattern: RecurrencePattern
    range: RecurrenceRange


@dataclass
class TaskList:
    """A MS To Do task list."""

    list_id: str
    name: str
    is_owner: bool = True
    is_shared: bool = False


@dataclass
class TaskSubscriptionConfig:
    """Tracks a single Graph API webhook subscription for one task list."""

    sub_id: str
    list_id: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Task (MS To Do representation — NOT stored in Firestore)
# ---------------------------------------------------------------------------


class Task(BaseModel):
    """
    Full MS To Do task. Returned by TasksProviderPort methods.
    Not stored in Firestore — lives in MS To Do.
    """

    # Identity (MS-assigned IDs)
    task_id: str
    list_id: str
    list_name: str
    user_id: str  # injected by adapter at construction time for routing

    # Content
    title: str
    body: Optional[str] = None

    # Dates
    due_datetime: Optional[datetime] = None
    start_datetime: Optional[datetime] = None
    reminder_datetime: Optional[datetime] = None
    is_reminder_on: bool = False
    completed_at: Optional[datetime] = None

    # Classification
    importance: TaskImportance = TaskImportance.NORMAL
    status: TaskStatus = TaskStatus.NOT_STARTED
    tags: List[str] = []
    recurrence: Optional[TaskRecurrence] = None

    # Structure
    checklist_items: List[ChecklistItem] = []
    attachments: List[TaskAttachment] = []
    linked_resources: List[LinkedResource] = []

    # Lifecycle
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class TaskCreate(BaseModel):
    """Input for creating a task. list_id is optional (defaults to primary list)."""

    title: str
    list_id: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[datetime] = None
    start_datetime: Optional[datetime] = None
    reminder_datetime: Optional[datetime] = None
    is_reminder_on: bool = False
    importance: TaskImportance = TaskImportance.NORMAL
    tags: List[str] = []
    recurrence: Optional[TaskRecurrence] = None
    checklist_items: List[ChecklistItem] = []
    linked_resources: List[LinkedResource] = []

    model_config = {"arbitrary_types_allowed": True}


class TaskUpdate(BaseModel):
    """All fields optional. Only set fields are sent to Graph API (PATCH semantics)."""

    title: Optional[str] = None
    body: Optional[str] = None
    due_datetime: Optional[datetime] = None
    start_datetime: Optional[datetime] = None
    reminder_datetime: Optional[datetime] = None
    is_reminder_on: Optional[bool] = None
    importance: Optional[TaskImportance] = None
    status: Optional[TaskStatus] = None
    tags: Optional[List[str]] = None
    recurrence: Optional[TaskRecurrence] = None
    checklist_items: Optional[List[ChecklistItem]] = None
    linked_resources: Optional[List[LinkedResource]] = None

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Search index entry (stored in Firestore)
# ---------------------------------------------------------------------------


class TaskSearchEntry(BaseModel):
    """
    Stored in Firestore search index. Thin record — enough to search and display
    results without fetching from Graph API.
    """

    task_id: str
    list_id: str
    list_name: str
    user_id: str
    title: str
    status: TaskStatus
    tags: List[str] = []
    importance: TaskImportance = TaskImportance.NORMAL
    short_id: str = ""  # stable 8-char md5 prefix, used by TasksAgent instead of full task_id

    content_vector: Optional[List[float]] = None
    # embed: "{title}. {body}. {' '.join(item.title for item in checklist_items)}"

    context_vector: Optional[List[float]] = None
    # embed: "{list_name}. {', '.join(tags)}. Importance: {importance}"

    indexed_at: datetime


# ---------------------------------------------------------------------------
# Per-user config (stored in Firestore — infrastructure config, not a business entity)
# ---------------------------------------------------------------------------


@dataclass
class TaskUserConfig:
    """
    Per-user tasks integration config stored in Firestore via TaskConfigPort.
    Collection: {env}_task_config/{user_id}.
    """

    primary_list_id: Optional[str] = None
    subscriptions: List[TaskSubscriptionConfig] = field(default_factory=list)
