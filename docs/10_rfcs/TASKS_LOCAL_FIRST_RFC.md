# RFC: Microsoft To Do Integration with Thin Search Index

**Status:** IMPLEMENTED (2026-03-19)
**Date:** 2026-03-18
**Supersedes:** `TASKS_AGENT_RFC.md` (Google Tasks adapter approach)

---

## 1. Problem Statement

The current `TasksAgent` uses Google Tasks via `TasksProviderPort`. Pain points:
- API is poor: no importance, reminders, recurrence, checklist items, linked resources
- `search_tasks` is client-side substring filter — no semantic understanding
- OAuth token expiry issues in dev

## 2. Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Provider | Microsoft To Do | Rich Graph API; all desired fields present natively |
| Source of truth | MS To Do (not Firestore) | No double bookkeeping; MS To Do is the real DB |
| Firestore role | Thin search index only (vectors + task_id pointers) | Adds semantic search without full mirror complexity |
| Lists | Single default list ("Alek Bot Tasks") | LLM always writes to default; multi-list deferred until sharing use case |
| Classification | Tags (MS To Do categories) | Primary grouping mechanism; composable, semantic-search-friendly, no LLM ambiguity |
| Sharing | Deferred — via MS To Do list sharing natively when needed | No per-task assignee in MS To Do; multi-list support is the enabler |
| Sync from app | Webhooks (Graph subscriptions) for search index freshness | Re-index on change; not syncing full data |
| Agent type | LLM tool-calling agent (current pattern) | Orchestrator delegates, agent autonomously picks tools |
| Google Tasks | Frozen, not removed | Preserve existing code; migrate users to MS To Do |

---

## 3. Architecture

```
User ──Slack/Telegram──► TasksAgent (LLM tool-calling, max N turns)
                              │
                ┌─────────────┼──────────────────┐
                ▼             ▼                  ▼
      TasksProviderPort   TaskSearchIndex    (embed service)
      (MS To Do CRUD)     (Firestore vectors)
                │
          Graph API
          MS To Do app  ← user manages here
```

**Data flow for CRUD:**
```
create_task:
  POST Graph API → ms_task_id
  embed(title + body + checklist) → upsert TaskSearchIndex entry

update_task:
  PATCH Graph API
  re-embed → update TaskSearchIndex entry

delete_task:
  DELETE Graph API
  delete from TaskSearchIndex

search_tasks:
  embed(query) → find_nearest in TaskSearchIndex → list of (task_id, list_id)
  batch fetch full tasks from Graph API
  return rich Task objects

list_tasks:
  GET Graph API directly (no Firestore involved)
```

**Webhook flow (changes from MS To Do app):**
```
User edits task in MS To Do → Graph sends change notification
  → POST /webhook/microsoft-tasks/{user_id}
  → fetch updated task from Graph API
  → re-embed → update TaskSearchIndex entry
```

---

## 4. Domain Model (`src/domain/task.py`)

Replaces existing `task.py`. Rich model representing MS To Do data. Not stored in Firestore
(Firestore only stores `TaskSearchEntry` and `TaskUserConfig`).

### 4.1 Enums

```python
class TaskStatus(str, Enum):
    """Mirrors MS To Do taskStatus enum directly."""
    NOT_STARTED       = "notStarted"
    IN_PROGRESS       = "inProgress"
    WAITING_ON_OTHERS = "waitingOnOthers"
    DEFERRED          = "deferred"
    COMPLETED         = "completed"

class TaskImportance(str, Enum):
    LOW    = "low"
    NORMAL = "normal"
    HIGH   = "high"
```

### 4.2 Value Objects

```python
@dataclass
class ChecklistItem:
    """Mirrors MS To Do checklistItem."""
    item_id: str                           # MS-assigned ID (used for updates/deletes)
    title: str
    is_completed: bool = False
    created_at: Optional[datetime] = None
    checked_at: Optional[datetime] = None  # MS: checkedDateTime

@dataclass
class TaskAttachment:
    """
    File attached to a task.
    - Uploaded via bot: stored in GCS, pushed to MS To Do as base64 (max 3 MB per MS limit)
    - External link: url field only
    GCS path: gs://{project}-task-attachments/{master_account_id}/{user_account_id}/{task_id}/{filename}
    """
    attachment_id: str    # MS-assigned ID
    filename: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    gcs_uri: Optional[str] = None    # set when uploaded via bot
    url: Optional[str] = None        # set when external link

@dataclass
class LinkedResource:
    """Mirrors MS To Do linkedResource."""
    resource_id: str                         # MS-assigned ID
    web_url: Optional[str] = None
    display_name: Optional[str] = None
    application_name: Optional[str] = None  # source app ("Outlook", "Jira", etc.)
    external_id: Optional[str] = None       # ID in source system

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
    index: Optional[str] = None   # "first"|"second"|"third"|"fourth"|"last"

@dataclass
class RecurrenceRange:
    """
    Mirrors MS To Do recurrenceRange.
    type: "endDate" | "noEnd" | "numbered"
    """
    type: str
    start_date: str                              # YYYY-MM-DD
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

@dataclass
class TaskUserConfig:
    """
    Per-user tasks integration config stored in Firestore via TaskConfigPort.
    Collection: {env}_task_config/{user_id}.
    Not a business entity — infrastructure config only.
    """
    primary_list_id: Optional[str] = None
    subscriptions: List[TaskSubscriptionConfig] = field(default_factory=list)
```

### 4.3 Task (MS To Do representation)

```python
class Task(BaseModel):
    """
    Full MS To Do task. Returned by TasksProviderPort methods.
    Not stored in Firestore — lives in MS To Do.
    """
    # Identity (MS-assigned IDs)
    task_id: str         # MS To Do task ID
    list_id: str         # MS To Do list ID
    list_name: str       # for display; denormalized from list
    user_id: str         # NOT from MS To Do; injected by adapter at construction time for routing

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
    tags: List[str] = []                    # MS To Do: categories
    recurrence: Optional[TaskRecurrence] = None

    # Structure
    checklist_items: List[ChecklistItem] = []
    attachments: List[TaskAttachment] = []
    linked_resources: List[LinkedResource] = []

    # Lifecycle (from MS)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # ── Future local extensions (not implemented yet) ──────────────────────
    # When needed: store in separate {env}_task_local_metadata collection
    # keyed by (user_id, task_id) and merge into Task on fetch.
    # assigned_to: Optional[str] = None    # local assignee
    # watchers: List[str] = []             # notification recipients
    # parent_task_id: Optional[str] = None # subtask hierarchy
    # custom_fields: Dict[str, Any] = {}   # arbitrary metadata
```

### 4.4 Input models

```python
class TaskCreate(BaseModel):
    """Input for creating a task. list_id is optional (defaults to primary list)."""
    title: str
    list_id: Optional[str] = None         # if None: use user's primary "Alek Bot Tasks" list
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
```

### 4.5 Search index entry

```python
class TaskSearchEntry(BaseModel):
    """
    Stored in Firestore search index. Thin record — enough to search and display
    results without fetching from Graph API.
    """
    task_id: str          # MS To Do task ID (= doc ID suffix)
    list_id: str
    list_name: str        # denormalized from Task.list_name; used in context_vector embed
    user_id: str
    title: str            # for display in search results
    status: TaskStatus    # for filtering (exclude completed by default)
    tags: List[str] = []  # for context embed

    importance: TaskImportance = TaskImportance.NORMAL
    # stored to keep TaskSearchEntry self-contained; also used in context_vector embed

    short_id: str = ""
    # stable 8-char md5 prefix, used by TasksAgent as task_ref instead of full 180-char MS IDs

    content_vector: Optional[List[float]] = None
    # embed: "{title}. {body}. {' '.join(item.title for item in checklist_items)}"

    context_vector: Optional[List[float]] = None
    # embed: "{list_name}. {', '.join(tags)}. Importance: {importance}"

    indexed_at: datetime
```

---

## 5. Ports

### 5.1 `TasksProviderPort` (extend existing `src/ports/tasks_provider_port.py`)

Add to the existing ABC:

```python
@abstractmethod
async def list_task_lists(self, user_id: str) -> List[TaskList]:
    """Return all task lists for the user."""

@abstractmethod
async def get_task(self, user_id: str, list_id: str, task_id: str) -> Task:
    """Fetch single task by ID. Raises ValueError if not found."""

@abstractmethod
async def batch_get_tasks(
    self, user_id: str, task_refs: List[Tuple[str, str]]  # [(list_id, task_id)]
) -> List[Task]:
    """Fetch multiple tasks across lists. Used after search_index lookup."""
```

Updated signatures for existing methods:

```python
@abstractmethod
async def list_tasks(
    self, user_id: str, list_id: Optional[str] = None, show_completed: bool = False
) -> List[Task]:
    """List tasks. list_id=None returns tasks across all lists."""

@abstractmethod
async def create_task(self, user_id: str, task: TaskCreate) -> Task: ...

@abstractmethod
async def update_task(
    self, user_id: str, list_id: str, task_id: str, updates: TaskUpdate
) -> Task: ...

@abstractmethod
async def delete_task(self, user_id: str, list_id: str, task_id: str) -> None: ...
```

**`search_tasks` is removed from the port.** No callers exist after this refactor.
Removing it avoids a broken-contract situation where `MicrosoftToDoAdapter` would need to
raise `NotImplementedError` on an abstract method. `GoogleTasksAdapter` retains its
own client-side implementation internally but is no longer exposed via the port.

### 5.2 `TaskSearchIndex` (`src/ports/task_search_index.py`) — NEW

```python
class TaskSearchIndex(ABC):

    @abstractmethod
    async def upsert(self, entry: TaskSearchEntry) -> None:
        """Insert or replace search index entry for a task."""

    @abstractmethod
    async def delete(self, user_id: str, task_id: str) -> None:
        """Remove task from search index."""

    @abstractmethod
    async def delete_by_list(self, user_id: str, list_id: str) -> None:
        """Remove all tasks in a list from the index (used when list is deleted)."""

    @abstractmethod
    async def find_nearest(
        self,
        user_id: str,
        vectors: Dict[str, List[float]],
        limit: int = 10,
        show_completed: bool = False,
        list_id: Optional[str] = None,
    ) -> List[TaskSearchEntry]:
        """
        RRF vector search. Returns TaskSearchEntry list (caller fetches full tasks from Graph).
        show_completed=False filters out status==COMPLETED.
        list_id if set restricts search to one list.
        """

    @abstractmethod
    async def get_by_short_id(self, user_id: str, short_id: str) -> Optional[TaskSearchEntry]:
        """Lookup by short_id (md5(task_id)[:8]). Returns None if not found."""

    @abstractmethod
    async def delete_all_for_user(self, user_id: str) -> None:
        """Remove all task index entries for a user. Called on disconnect."""
```

### 5.3 `TaskConfigPort` (`src/ports/task_config_port.py`) — NEW

Stores per-user tasks integration config (primary list ID + active subscriptions).
Single external boundary: Firestore (implemented by `FirestoreTaskConfigRepository`).
`MicrosoftToDoAdapter` receives this via constructor — does not write Firestore directly.

```python
class TaskConfigPort(ABC):

    @abstractmethod
    async def get_config(self, user_id: str) -> TaskUserConfig:
        """Load user's task config. Returns empty TaskUserConfig if not found."""

    @abstractmethod
    async def save_config(self, user_id: str, config: TaskUserConfig) -> None:
        """Overwrite user's task config."""

    @abstractmethod
    async def set_primary_list_id_if_absent(self, user_id: str, list_id: str) -> str:
        """
        Atomic create-if-not-exists for primary_list_id.
        If primary_list_id is already set, returns the existing value unchanged.
        If not set, writes list_id and returns it.
        Implemented as a Firestore transaction — safe under concurrent calls.
        """
```

### 5.4 `TaskLifecyclePort` (`src/ports/task_lifecycle_port.py`) — NEW

Graph API operations for subscription management and initial list setup.
Separated from `TasksProviderPort` to keep task CRUD distinct from integration
lifecycle concerns. `MicrosoftToDoAdapter` implements both ports.
Config persistence (writing results to Firestore) is the **caller's** responsibility
(`TaskSetupService`) — these methods only call Graph API and return results.

```python
class TaskLifecyclePort(ABC):

    @abstractmethod
    async def ensure_primary_list(self, user_id: str) -> str:
        """
        GET /me/todo/lists → find "Alek Bot Tasks" → if absent POST to create.
        Returns list_id. Does NOT persist — caller persists via TaskConfigPort.
        """

    @abstractmethod
    async def register_subscription(
        self, user_id: str, list_id: str, notification_url_base: str
    ) -> TaskSubscriptionConfig:
        """
        POST /subscriptions for the given list.
        Returns TaskSubscriptionConfig(sub_id, list_id, expires_at).
        Does NOT persist — caller persists via TaskConfigPort.
        """

    @abstractmethod
    async def renew_subscription(
        self, user_id: str, sub_id: str
    ) -> TaskSubscriptionConfig:
        """
        PATCH /subscriptions/{sub_id} with new expirationDateTime.
        Returns updated TaskSubscriptionConfig.
        Does NOT persist — caller persists via TaskConfigPort.
        """

    @abstractmethod
    async def delete_subscription(self, user_id: str, sub_id: str) -> None:
        """DELETE /subscriptions/{sub_id}."""
```

---

## 6. Adapters

### 6.1 `MicrosoftToDoAdapter` (`src/adapters/microsoft_todo_adapter.py`) — NEW

Implements `TasksProviderPort` **and** `TaskLifecyclePort`.

**Constructor:**
```python
def __init__(
    self,
    oauth_credentials: OAuthCredentialsPort,
    task_config: TaskConfigPort,           # injected; adapter never writes Firestore directly
    client_id: str,
    client_secret: str,
)
```

**Auth:** OAuth via `OAuthCredentialsPort` (provider="microsoft_todo"). Token refresh pattern
mirrors `GmailProviderAdapter`. Refresh endpoint:
`https://login.microsoftonline.com/consumers/oauth2/v2.0/token`.

**Graph API base:** `https://graph.microsoft.com/v1.0/me/todo/`

**List management:**
```
list_task_lists():    GET /me/todo/lists → List[TaskList]
ensure_primary_list(): GET /me/todo/lists → find "Alek Bot Tasks" → if absent: POST to create.
                        Returns list_id. Does NOT persist result.
                        Caller (TaskSetupService) persists via task_config.set_primary_list_id_if_absent().
                        Per-instance in-memory cache (list_id) as a hot-path optimization:
                        populated on first call from task_config.get_config(); subsequent calls use cache.
```

**Task CRUD field mapping:**

| Local `Task` field | Graph API field | Notes |
|--------------------|----------------|-------|
| `title` | `title` | direct |
| `body` | `body.content` + `body.contentType: "text"` | |
| `status` | `status` | all 5 values map 1:1 |
| `importance` | `importance` | direct |
| `due_datetime` | `dueDateTime.dateTime` + `.timeZone: "UTC"` | |
| `start_datetime` | `startDateTime.dateTime` + `.timeZone: "UTC"` | |
| `reminder_datetime` | `reminderDateTime.dateTime` + `.timeZone: "UTC"` | |
| `is_reminder_on` | `isReminderOn` | |
| `completed_at` | `completedDateTime.dateTime` + TZ | only when status=COMPLETED |
| `tags` | `categories` | |
| `recurrence` | `recurrence` (patternedRecurrence) | `TaskRecurrence` mirrors structure; direct mapping |
| `checklist_items` | `checklistItems` | On create: POST full array. On update: diff against existing items by `item_id` — PATCH changed, DELETE removed, POST new. Full array replace must not be used: it destroys `checked_at` timestamps and breaks item_id continuity. |
| `linked_resources` | `linkedResources[{webUrl, displayName, applicationName, externalId}]` | |
| `attachments` | `POST /tasks/{id}/attachments` | separate call; base64 contentBytes from GCS |

**`batch_get_tasks`:** Issues individual fetches via
`GET /me/todo/lists/{list_id}/tasks/{task_id}` per ref. Concurrency capped at 5
via `asyncio.Semaphore` to stay within Graph API throttling limits for personal accounts
(consumers tenant). 429 responses: retry with exponential backoff, max 3 attempts.
O(k) regardless of list size — cleaner and more predictable than fetching entire lists
and filtering client-side.

**Subscription Graph API calls** (implements `TaskLifecyclePort`):
```
register_subscription(user_id, list_id, notification_url_base):
  POST /subscriptions {changeType: "created,updated,deleted",
                       resource: "/me/todo/lists/{list_id}/tasks",
                       expirationDateTime: now+4319min,
                       notificationUrl: "{notification_url_base}/webhook/microsoft-tasks/{user_id}",
                       clientState: "{webhook_secret}"}
  Returns TaskSubscriptionConfig(sub_id, list_id, expires_at).
  Does NOT persist — TaskSetupService persists via task_config.save_config().
  # user_id in webhook URL path → O(1) routing without Firestore reverse lookup.
  # clientState = shared secret for Graph signature validation only.

renew_subscription(user_id, sub_id):
  PATCH /subscriptions/{sub_id} {expirationDateTime: now+4319min}
  Returns updated TaskSubscriptionConfig. Does NOT persist.

delete_subscription(user_id, sub_id):
  DELETE /subscriptions/{sub_id}
```

Subscriptions are per-list. Config persistence (`{env}_task_config/{user_id}`) is managed
exclusively by `TaskSetupService` via `TaskConfigPort` — the adapter is unaware of Firestore.

### 6.2 `FirestoreTaskSearchIndex` (`src/adapters/firestore_task_search_index.py`) — NEW

- Collection: `{env}_task_search_index` (doc ID = `{user_id}_{task_id}`)
- `_wrap_vectors()` / `_unwrap_vectors()` — same pattern as email repo
- RRF search across `content_vector` + `context_vector`
- Filter: `user_id == user_id`, optionally `list_id == list_id`, optionally `status != "completed"`
- `delete_all_for_user`: batch delete all docs where `user_id == user_id`

### 6.3 `FirestoreTaskConfigRepository` (`src/adapters/firestore_task_config_repository.py`) — NEW

- Collection: `{env}_task_config` (doc ID = `user_id`)
- Implements `TaskConfigPort`
- `get_config`: read doc → deserialize to `TaskUserConfig`; return empty config if doc absent
- `save_config`: write doc (full overwrite)
- `set_primary_list_id_if_absent`: Firestore transaction — if `primary_list_id` field absent:
  write `list_id` and return it; if already set: return existing value. Safe under concurrent calls.

---

## 7. Agent + Services

### 7.1 Agent changes (`src/agents/tasks_agent.py`)

**New constructor dependencies:**
```python
def __init__(
    self,
    config: AgentConfig,
    tasks_provider: TasksProviderPort,       # replaces old provider
    task_indexing: TaskIndexingService,      # NEW — replaces search_index + embedding_service
    prompt_builder: PromptBuilderPort,
)
```

**LLM tool set:**

`list_task_lists` is **not exposed to the LLM**. The port method exists; enabling it is a
one-line change to the agent manifest when multi-list sharing becomes a real use case.

**Tool: `list_tasks`**
- `tasks_provider.list_tasks(user_id, list_id=None, show_completed=False)` — direct Graph call
- `list_id` is never passed by the LLM (always queries primary list via `list_id=None`)

**Tool: `search_tasks`**
- `task_indexing.search(user_id, query)` → `List[TaskSearchEntry]`
- `tasks_provider.batch_get_tasks(user_id, refs)` → full Task objects
- Return formatted results

**Tool: `create_task`**
- LLM provides: `title`, `body`, `due_datetime`, `importance`, `tags`, `checklist_items`, `recurrence` (optional)
- `list_id` is **never passed by LLM** — adapter always uses `primary_list_id`
- Tags are the primary classification mechanism (map to MS To Do `categories`)
- LLM is instructed to auto-tag from context (e.g. "remind me about Prague hotel" → tags: ["prague", "trip"])
- Recurrence: 5 patterns supported — `daily`, `weekdays` (Mon–Fri alias), `weekly`, `absoluteMonthly`, `absoluteYearly`. `relativeMonthly`/`relativeYearly` excluded (Graph API bug: silently converts to daily). Smart defaults from `due_datetime` when optional fields omitted.
- `tasks_provider.create_task(user_id, task_create)` → `Task`
- `task_indexing.index_task(task)`

**Tool: `update_task`**
- LLM passes `task_ref` (short_id, 8 chars) from prior `search_tasks` result. Agent resolves to `(list_id, task_id)` via `task_indexing.resolve_short_id()`.
- `tasks_provider.update_task(user_id, list_id, task_id, updates)` → `Task`
- `task_indexing.index_task(task)`

**Tool: `delete_task`**
- LLM passes `task_ref` (short_id, 8 chars) from prior `search_tasks` result. Agent resolves to `(list_id, task_id)` via `task_indexing.resolve_short_id()`.
- `tasks_provider.delete_task(user_id, list_id, task_id)`
- `task_indexing.deindex_task(user_id, task_id)`

**Multi-list upgrade path:** When sharing use case arrives — add `list_task_lists` +
`create_list` tools to agent manifest and update `create_task` schema to accept `list_id`.
No domain or adapter changes required.

**⚠️ Test impact:** `test_tasks_agent.py` breaks — constructor changes. Each failing test
needs per-test approval before modification.

---

### 7.2 `TaskIndexingService` (`src/services/task_indexing_service.py`) — NEW

Encapsulates the embed→index pipeline. Single implementation — no port needed.
Used by: `TasksAgent` (CRUD), webhook handler, `WorkerHandler` (reindex).

```python
class TaskIndexingService:
    def __init__(
        self,
        embedding_service: EmbeddingService,
        search_index: TaskSearchIndex,
        tasks_provider: TasksProviderPort,   # for fetch-then-index operations
    ): ...
```

**Methods:**

```
index_task(task: Task) -> None
    embed(title + body + checklist_items) → content_vector
    embed(list_name + tags + importance)  → context_vector
    search_index.upsert(TaskSearchEntry)

deindex_task(user_id: str, task_id: str) -> None
    search_index.delete(user_id, task_id)

index_task_by_ref(user_id: str, list_id: str, task_id: str) -> None
    tasks_provider.get_task(user_id, list_id, task_id) → index_task(task)
    Used by webhook handler on created/updated notifications.

reindex_list(user_id: str, list_id: str) -> None
    tasks_provider.list_tasks(user_id, list_id, show_completed=True) → List[Task]
    index_task() for each task in parallel (bounded concurrency)
    Used by WorkerHandler reindex_task_list.

search(user_id: str, query: str, show_completed: bool = False,
       list_id: Optional[str] = None) -> List[TaskSearchEntry]
    embed(query) → query_vector
    search_index.find_nearest(user_id, {content: query_vector, context: query_vector}, ...)
    Used by TasksAgent search_tasks tool.

resolve_short_id(user_id: str, short_id: str) -> Tuple[str, str]
    search_index.get_by_short_id(user_id, short_id) → TaskSearchEntry
    Returns (list_id, task_id). Raises ValueError if not found.
    Used by TasksAgent for update_task and delete_task.
```

---

### 7.3 `TaskSetupService` (`src/services/task_setup_service.py`) — NEW

Orchestrates integration lifecycle: setup, disconnect, subscription management, status,
reindex. Called by `WorkerHandler`, `microsoft_tasks_webhook.py`, `user_cabinet_app.py`.
No port needed — single implementation.

```python
class TaskSetupService:
    def __init__(
        self,
        lifecycle: TaskLifecyclePort,
        task_config: TaskConfigPort,
        tasks_provider: TasksProviderPort,     # for list_task_lists in ensure_subscriptions
        oauth_credentials: OAuthCredentialsPort,
        task_search_index: TaskSearchIndex,
        task_queue: TaskQueue,
        notification_url_base: str,
    ): ...
```

**Methods:**

```
setup(user_id: str) -> None
    lifecycle.ensure_primary_list(user_id) → list_id
    task_config.set_primary_list_id_if_absent(user_id, list_id)
    ensure_subscriptions(user_id)

ensure_subscriptions(user_id: str) -> None
    config = task_config.get_config(user_id)
    tasks_provider.list_task_lists(user_id) → lists
    for each list:
        if no active sub or sub.expires_at < now:
            sub = lifecycle.register_subscription(user_id, list.list_id, notification_url_base)
            config.subscriptions.append(sub)
            task_queue.enqueue("reindex_task_list", {user_id, list_id})
    task_config.save_config(user_id, config)

handle_subscription_renewal(user_id: str, sub_id: str) -> None
    config = task_config.get_config(user_id)
    sub = find sub by sub_id in config.subscriptions
    if sub.expires_at - now < 48h:
        updated = lifecycle.renew_subscription(user_id, sub_id)
        replace sub in config.subscriptions with updated
        task_config.save_config(user_id, config)

renew_expiring_subscriptions(user_id: str) -> None
    config = task_config.get_config(user_id)
    for each sub where expires_at - now < 24h:
        updated = lifecycle.renew_subscription(user_id, sub.sub_id)
        replace sub in config
    task_config.save_config(user_id, config)

disconnect(user_id: str) -> None
    config = task_config.get_config(user_id)
    for each sub: lifecycle.delete_subscription(user_id, sub.sub_id)
    oauth_credentials.delete(user_id, provider="microsoft_todo")
    task_search_index.delete_all_for_user(user_id)
    task_config.save_config(user_id, TaskUserConfig())  # clear config

get_status(user_id: str) -> dict
    connected = oauth_credentials.exists(user_id, provider="microsoft_todo")
    config = task_config.get_config(user_id)
    → {connected, subscriptions: [{list_id, expires_at}]}

reindex_all(user_id: str) -> None
    ensure_subscriptions(user_id)  # repairs any expired subs too
    config = task_config.get_config(user_id)
    for each sub: task_queue.enqueue("reindex_task_list", {user_id, list_id})
```

---

## 8. Webhook: Search Index Freshness

When user edits a task directly in MS To Do app, the search index needs re-indexing.

The webhook blueprint factory receives `task_indexing: TaskIndexingService` and
`task_setup: TaskSetupService` as constructor args — no direct access to ports.

```
POST /webhook/microsoft-tasks/{user_id}
├─ ?validationToken=XYZ → return XYZ as text/plain 200 (Graph validation)
└─ Change notification:
     ├─ Verify clientState == webhook_secret
     ├─ Extract sub_id, list_id, ms_task_id from notification
     │    (ms_task_id from resourceData["@odata.id"]: "/me/todo/lists/{list_id}/tasks/{task_id}")
     ├─ user_id extracted from URL path — no Firestore reverse lookup required
     ├─ changeType == "deleted":
     │    task_indexing.deindex_task(user_id, ms_task_id)
     └─ changeType == "created"|"updated":
          task_indexing.index_task_by_ref(user_id, list_id, ms_task_id)
     ├─ Self-healing renewal:
     │    task_setup.handle_subscription_renewal(user_id, sub_id)
     Return 202 Accepted immediately
```

**Subscription liveness strategy (three layers):**

1. **Self-healing on webhook receipt** — every incoming notification calls
   `task_setup.handle_subscription_renewal()`. Active users auto-renew without relying on Scheduler.

2. **Idempotent `ensure_subscriptions(user_id)`** — called by `TaskSetupService.setup()` and
   `TaskSetupService.reindex_all()`. Repairs missing or expired subscriptions; enqueues reindex
   per affected list.

3. **Cabinet visibility** — `GET /api/tasks/status` returns `subscriptions: [{list_id, expires_at}]`.
   Cabinet UI should flag subscriptions that are expired.

---

## 9. OAuth (`src/web/oauth_app.py`)

```
GET /auth/connect-microsoft-todo
└─ Azure consumers endpoint
└─ Scopes: Tasks.ReadWrite offline_access
└─ CSRF cookies: microsoft_todo_oauth_state, microsoft_todo_connect_user_id

GET /auth/connect-microsoft-todo/callback
└─ Exchange code → save OAuthCredentials(provider="microsoft_todo")
└─ Enqueue Cloud Task: task_type="setup_microsoft_todo"
     → ensure_primary_list("Alek Bot Tasks") + register_subscription per list
└─ Redirect /cabinet?microsoft_todo_connected=1
```

Azure app: Personal Microsoft accounts only (`consumers` tenant), `Tasks.ReadWrite` delegated.

---

## 10. Cabinet API (`src/web/user_cabinet_app.py`)

All Cabinet endpoints delegate to `TaskSetupService` or `TasksProviderPort` — no port calls
in the web handler directly.

```
GET    /api/tasks/status
       → task_setup.get_status(user_id)
       → {connected: bool, subscriptions: [{list_id, expires_at}]}

POST   /api/tasks/reindex
       → task_setup.reindex_all(user_id)   # enqueues reindex_task_list tasks per list

GET    /api/tasks/lists
       → tasks_provider.list_task_lists(user_id)   # proxy to Graph API; read-only

DELETE /api/tasks/disconnect
       → task_setup.disconnect(user_id)
```

---

## 11. Configuration

```python
# src/config/settings.py additions
microsoft_todo_client_id: str = ""
microsoft_todo_client_secret: str = ""
microsoft_todo_redirect_uri: str = ""
microsoft_tasks_webhook_secret: str = ""
```

WorkerHandler constructor gains two optional deps (same optional pattern as `task_queue`, `media_storage`):
```python
task_setup: Optional[TaskSetupService] = None
task_indexing: Optional[TaskIndexingService] = None
```

WorkerHandler task types:
```python
"setup_microsoft_todo"         → task_setup.setup(user_id)
                                  # idempotent: ensure_primary_list + ensure_subscriptions
                                  # ensure_subscriptions enqueues reindex_task_list per list

"reindex_task_list"            → task_indexing.reindex_list(user_id, list_id)
                                  # fetches tasks from Graph, embeds, upserts index

"renew_task_subscriptions"     → task_setup.renew_expiring_subscriptions(user_id)
                                  # secondary defense; primary is self-healing on webhook receipt
```

---

## 12. Files to Create

| File | Purpose |
|------|---------|
| `src/ports/task_search_index.py` | `TaskSearchIndex` ABC |
| `src/ports/task_config_port.py` | `TaskConfigPort` ABC |
| `src/ports/task_lifecycle_port.py` | `TaskLifecyclePort` ABC |
| `src/adapters/microsoft_todo_adapter.py` | Graph API CRUD + subscriptions; implements `TasksProviderPort` + `TaskLifecyclePort` |
| `src/adapters/firestore_task_search_index.py` | Firestore vector search index; implements `TaskSearchIndex` |
| `src/adapters/firestore_task_config_repository.py` | Firestore per-user config; implements `TaskConfigPort` |
| `src/services/task_indexing_service.py` | `TaskIndexingService` — embed+index pipeline |
| `src/services/task_setup_service.py` | `TaskSetupService` — setup/disconnect/subscription orchestration |
| `src/web/microsoft_tasks_webhook.py` | Webhook blueprint factory — `POST /webhook/microsoft-tasks/{user_id}`; mirrors `deep_research_webhooks.py` pattern |

## 13. Files to Modify

| File | Change |
|------|--------|
| `src/domain/task.py` | Full replacement (rich model + TaskSearchEntry) |
| `src/ports/tasks_provider_port.py` | Add `list_task_lists`, `get_task`, `batch_get_tasks`; remove `search_tasks`; update existing signatures |
| `src/agents/tasks_agent.py` | New constructor, semantic search, rich tools; tags as classification; single default list; `list_task_lists` not exposed to LLM |
| `src/composition/user_agent_factory.py` | Pass `task_indexing: TaskIndexingService` to TasksAgent constructor (replaces `search_index` + `embedding_service`) |
| `src/web/oauth_app.py` | MS OAuth routes |
| `src/web/user_cabinet_app.py` | `/api/tasks/*` — delegates to `TaskSetupService` / `TasksProviderPort` |
| `src/composition/service_container.py` | Deactivate `GoogleTasksAdapter`; wire `MicrosoftToDoAdapter`, `FirestoreTaskSearchIndex`, `FirestoreTaskConfigRepository`, `TaskIndexingService`, `TaskSetupService` |
| `src/config/settings.py` | MS OAuth config fields |
| `src/handlers/worker_handler.py` | Add `setup_microsoft_todo`, `reindex_task_list`, `renew_task_subscriptions` task types; constructor gains `task_setup: Optional[TaskSetupService] = None`, `task_indexing: Optional[TaskIndexingService] = None` (same optional pattern as existing deps) |

**`GoogleTasksAdapter`:** Frozen. No changes to the file. However, because the port signatures
for `update_task` and `delete_task` gain a `list_id` parameter, the adapter can no longer
satisfy the ABC — it would raise `TypeError` at instantiation. Resolution: remove
`GoogleTasksAdapter` instantiation from `service_container.py` (Phase 1, alongside port changes).
`tasks_provider` passed as `None` to `UserAgentFactory` until `MicrosoftToDoAdapter` is wired
in Phase 5. No real users on Google Tasks.

---

## 14. Tests to Create

| File | Key scenarios |
|------|---------------|
| `tests/unit/ports/test_task_search_index.py` | ABC contract: `TaskSearchIndex` |
| `tests/unit/ports/test_task_config_port.py` | ABC contract: `TaskConfigPort` |
| `tests/unit/ports/test_task_lifecycle_port.py` | ABC contract: `TaskLifecyclePort` |
| `tests/unit/adapters/test_microsoft_todo_adapter.py` | Wire tests (mock httpx); field mapping; checklist diff logic; `ensure_primary_list` — no Firestore writes; `register_subscription` returns config without persisting |
| `tests/unit/adapters/test_firestore_task_search_index.py` | Vector wrap/unwrap; RRF; list_id filter; `delete_all_for_user` |
| `tests/unit/adapters/test_firestore_task_config_repository.py` | `set_primary_list_id_if_absent` transaction; `get_config` missing doc → empty config |
| `tests/unit/services/test_task_indexing_service.py` | `index_task` embed+upsert; `deindex_task`; `index_task_by_ref` fetches then indexes; `reindex_list`; `search` embed+find_nearest |
| `tests/unit/services/test_task_setup_service.py` | `setup`: ensure_primary_list + ensure_subscriptions + enqueue reindex; `disconnect` calls all three cleanup steps; `handle_subscription_renewal` renews only when < 48h; `renew_expiring_subscriptions` |
| `tests/unit/agents/test_tasks_agent.py` | NEW: `search_tasks` delegates to `task_indexing.search` → `batch_get_tasks`; `create_task` calls `task_indexing.index_task` after Graph; `delete_task` calls `task_indexing.deindex_task` |

---

## 15. Implementation Phases

### Phase 1 — Domain + Ports ✅ COMPLETED
- `src/domain/task.py` — full replacement (new model + `TaskSearchEntry` + `TaskUserConfig` + `TaskSubscriptionConfig`)
- `src/ports/task_search_index.py`
- `src/ports/task_config_port.py`
- `src/ports/task_lifecycle_port.py`
- Extend `src/ports/tasks_provider_port.py`
- Unit tests for all ports
- `src/composition/service_container.py` — deactivate `GoogleTasksAdapter` (alongside port signature changes)

### Phase 2 — Adapters ✅ COMPLETED
- `src/adapters/microsoft_todo_adapter.py` — implements `TasksProviderPort` + `TaskLifecyclePort`; injects `TaskConfigPort`
- `src/adapters/firestore_task_search_index.py`
- `src/adapters/firestore_task_config_repository.py`
- Wire tests for all three adapters

### Phase 3 — Services ✅ COMPLETED
- `src/services/task_indexing_service.py`
- `src/services/task_setup_service.py`
- Unit tests for both services

### Phase 4 — Agent ✅ COMPLETED
- `src/agents/tasks_agent.py` — new constructor with `task_indexing`
- `src/composition/user_agent_factory.py` — pass `task_indexing` to TasksAgent
- New agent unit tests; per-test resolution of old breakage

### Phase 5 — Web + config ✅ COMPLETED
- MS OAuth routes
- `src/web/microsoft_tasks_webhook.py` — delegate to `TaskIndexingService` + `TaskSetupService`
- Cabinet API — delegate to `TaskSetupService`
- Config fields
- WorkerHandler task types + `task_setup` / `task_indexing` deps

### Phase 6 — DI wiring + E2E ✅ COMPLETED
- `src/composition/service_container.py` — wire all new adapters + services; webhook blueprint
- E2E: connect MS To Do → create task via bot → appears in app + semantic search works

### Phase 6 — Documentation (post-implementation) ✅ COMPLETED
Update arc42 docs to reflect the implemented state. See §17.

---

## 16. Open Questions

1. **Subscription per list** — registering a Graph subscription per MS To Do list is required
   (can't subscribe to all lists at once). If user has 50 lists, that's 50 subscriptions.
   Graph quota: 1000 subscriptions per tenant for personal accounts. Not a concern for a
   personal exocortex, but worth noting.

2. **Initial search index population** — **resolved: YES.** `setup_microsoft_todo` Cloud Task
   performs an initial reindex of all existing tasks in all user lists after registering
   subscriptions. Without this, tasks created in MS To Do before connecting would be invisible
   to semantic search until manually reindexed. This is already covered by
   `ensure_subscriptions(user_id)` which enqueues a reindex per list when registering a new
   subscription (see §11).

3. **Attachment upload flow** — **resolved: YES.** When bot uploads a file: (a) upload to GCS,
   (b) read bytes, (c) base64 encode, (d) POST to Graph attachments API. MS limit is 3 MB.
   Files >3 MB: store in GCS only (set `url` field to a signed GCS URL), do not push to MS.

4. **Attachment read — TECH DEBT (not implemented).** MS Graph does not return attachments
   inline in `GET /tasks/{id}` — they require a separate `GET /tasks/{id}/attachments` call.
   Currently `MicrosoftToDoAdapter._task_from_ms()` does not fetch attachments; `Task.attachments`
   is always `[]`. Impact: `_format_task_list` in `TasksAgent` will never populate attachment
   filenames, so the LLM cannot surface them to the user.
   Fix when needed: add `_fetch_attachments(user_id, list_id, task_id)` in the adapter and call
   it from `_task_from_ms` (or lazily from a dedicated `get_task_attachments` tool).

---

## 17. Documentation Updates (post-implementation)

After all implementation phases are complete, update the following arc42 docs.

### New files to create

| File | Content |
|------|---------|
| `docs/05_building_blocks/tasks_integration/README.md` | Building block spec: local-first architecture, data flows, subscription lifecycle, agent integration, OAuth, Cabinet API. Mirror structure of `gmail_email_indexing/README.md`. |
| `docs/09_decisions/adr-008-local-first-external-provider/README.md` | ADR: external service as source of truth + thin Firestore search index. Captures rationale and rejected alternatives (full mirror). Reusable pattern for future integrations (Calendar, Notion, etc.). |

### Existing files to update

| File | What to add |
|------|-------------|
| `CLAUDE.md` | `TasksAgent` in Key Mechanisms; `MicrosoftToDoAdapter` + `TaskSearchIndex` in Architecture; `reindex_task_list` in WorkerHandler task types |
| `docs/08_concepts/DATABASE_SCHEMA.md` | Two new collections in §1.1 table + full schema sections: `{env}_task_search_index` (doc ID: `{user_id}_{task_id}`; fields + embed schemes) and `{env}_task_config` (doc ID: `user_id`; `primary_list_id`, `subscriptions`) |
| `docs/04_solution_strategy/current_implementation/STRUCTURE.md` | New files: `src/ports/task_search_index.py`, `src/adapters/microsoft_todo_adapter.py`, `src/adapters/firestore_task_search_index.py`; updated `src/domain/task.py` |
| `docs/05_building_blocks/multi_agent_system/README.md` | Add `TasksAgent` to specialist agents table: intent `manage_tasks`, tools, dependencies |
| `docs/05_building_blocks/oauth_multi_tenant/README.md` | Add Microsoft To Do as new OAuth provider: tenant `consumers`, scopes, refresh endpoint, provider key `"microsoft_todo"` |
| `docs/05_building_blocks/user_cabinet/README.md` | New Tasks Management section with `/api/tasks/status`, `/api/tasks/reindex`, `/api/tasks/lists`, `/api/tasks/disconnect` |
| `docs/06_runtime/API_REFERENCE.md` | OAuth routes (`/auth/connect-microsoft-todo/*`), webhook (`POST /webhook/microsoft-tasks/{user_id}`), Cabinet tasks API |
| `docs/12_risks/IMPLEMENTATION_ROADMAP.md` | Mark Tasks (MS To Do) integration as completed milestone |
| `docs/10_rfcs/TASKS_AGENT_RFC.md` | Add `**Status: SUPERSEDED by TASKS_LOCAL_FIRST_RFC.md (2026-03-18)**` at the top |

---

## 18. Implementation Notes (Deltas from RFC)

Implementation completed 2026-03-19. The following details differ from or extend the original RFC design.

### short_id system

`TaskSearchEntry.short_id` = `md5(task_id)[:8]`. Stable 8-char alias for the 180-char MS Graph task ID.
The LLM tool schema uses `task_ref` (short_id) for `update_task` and `delete_task` — never the full task_id.
`TaskIndexingService.resolve_short_id(user_id, short_id)` resolves to `(list_id, task_id)` via `TaskSearchIndex.get_by_short_id()`.
Rationale: full MS To Do task IDs are ~180 chars; LLM context window cost is significant when listing multiple tasks.

### Recurrence support

`create_task` tool exposes a `recurrence` object with 5 supported patterns:
- `daily` — every N days
- `weekdays` — Mon–Fri (convenience alias, maps to `weekly` with `days_of_week=["monday"..."friday"]`)
- `weekly` — specific days of week (defaults to weekday of `due_datetime` if not specified)
- `absoluteMonthly` — fixed day of month (defaults to day of `due_datetime`)
- `absoluteYearly` — fixed day + month (defaults to day+month of `due_datetime`)

`relativeMonthly` and `relativeYearly` are intentionally excluded: Graph API for personal (consumers) accounts silently converts them to `daily` — a known Microsoft bug.

`RecurrenceRange` is always `noEnd` with `start_date = today`. LLM does not need to specify range.

### Firestore transaction fix

`FirestoreTaskConfigRepository.set_primary_list_id_if_absent` uses `@firestore.async_transactional` on an inner function, not as a decorator on the method itself (Firestore async transaction pattern requires this).

### MAX_TURNS

`TasksAgent` uses `_MAX_TURNS = 4` (the constant used in the tool-calling loop). This is sufficient for search-before-mutate flows: search (1) → mutate (2) → format response (3), with one turn buffer for disambiguation.
