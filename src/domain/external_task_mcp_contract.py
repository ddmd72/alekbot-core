"""The worker MCP resource's tool contract: its tool names, each tool's parameter names, and the keys its two Firestore
tokens must carry (EXTERNAL_TASK_QUEUE_RFC §5.4).

The words live in the tokens `MCP_TASK_TOOL_TEXTS` and `MCP_TASK_RESULT_TEXTS`; this module only fixes their key sets,
which `ConnectorTexts.parse_bodies` checks at load (a missing key is an error, an unknown one is logged and ignored).
The tool token uses the drive connector's key shapes (`instructions`, `<tool>`, `<tool>.<param>`) because one
`TokenTextFastMCP` serves both apps.

No tool takes a user, an account, a worker or a label: each comes from the verified credential (§5.7).

Result keys:
- the claim, in its order: `CLAIMED` (task id, created time, lease id, lease end), `WRITTEN_BY` (who wrote the brief),
  `TITLE`, `TASK_TEXT` (the line after which the task text follows as written);
- `NO_TASK` (a claim that found nothing: a success, never an error);
- the listing: `LIST_HEAD`, one `LIST_QUEUED` or `LIST_LEASED` line per task, `LIST_EMPTY`;
- the lease and result answers: `RENEWED`, `SUBMITTED` (a replay of the same result answers the same), `RELEASED`,
  `FAILED`;
- the errors: `ID_REQUIRED` (an empty `task_id` or `lease_id`), `RATE_LIMITED`, one line per refusal a worker can meet
  (`E_NOT_FOUND`, …, keyed by `text_key_of_refusal`), and `REFUSED`, the one neutral line of `not_enabled`,
  `worker_revoked` and every other code.

`RESULT_PLACEHOLDERS` names the `{placeholders}` code fills in a result text; a key it does not list is printed as is.
Every refusal line carries `{code}`, the refusal's code, so a worker can branch on it. Pure: no I/O.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import FrozenSet, Mapping, Tuple

from .drive_connector_contract import INSTRUCTIONS_KEY, text_key_of_parameter
from .external_task_refusal import TaskRefusal

# -- tools ------------------------------------------------------------------------------------------------------------
LIST_TASKS = "list_tasks"
CLAIM_TASK = "claim_task"
RENEW_LEASE = "renew_lease"
SUBMIT_TASK_RESULT = "submit_task_result"
RELEASE_TASK = "release_task"
FAIL_TASK = "fail_task"

TOOL_NAMES: Tuple[str, ...] = (LIST_TASKS, CLAIM_TASK, RENEW_LEASE, SUBMIT_TASK_RESULT, RELEASE_TASK, FAIL_TASK)

TOOL_PARAMETERS: Mapping[str, Tuple[str, ...]] = MappingProxyType({
    LIST_TASKS: (),
    CLAIM_TASK: (),
    RENEW_LEASE: ("task_id", "lease_id"),
    SUBMIT_TASK_RESULT: ("task_id", "lease_id", "result"),
    RELEASE_TASK: ("task_id", "lease_id"),
    FAIL_TASK: ("task_id", "lease_id", "reason"),
})

# The one tool that changes nothing (`TokenTextFastMCP.read_only_tools`).
READ_ONLY_TOOLS: FrozenSet[str] = frozenset({LIST_TASKS})

REQUIRED_TOOL_KEYS: FrozenSet[str] = frozenset(
    {INSTRUCTIONS_KEY, *TOOL_NAMES,
     *(text_key_of_parameter(tool, param) for tool, params in TOOL_PARAMETERS.items() for param in params)})

# -- results ----------------------------------------------------------------------------------------------------------
CLAIMED = "CLAIMED"
WRITTEN_BY = "WRITTEN_BY"
TITLE = "TITLE"
TASK_TEXT = "TASK_TEXT"
NO_TASK = "NO_TASK"
LIST_HEAD = "LIST_HEAD"
LIST_QUEUED = "LIST_QUEUED"
LIST_LEASED = "LIST_LEASED"
LIST_EMPTY = "LIST_EMPTY"
RENEWED = "RENEWED"
SUBMITTED = "SUBMITTED"
RELEASED = "RELEASED"
FAILED = "FAILED"
ID_REQUIRED = "ID_REQUIRED"
RATE_LIMITED = "RATE_LIMITED"
REFUSED = "REFUSED"

# The refusals a worker tool can meet with a line of their own. `not_enabled` and `worker_revoked` answer `REFUSED`;
# `account_locked` answers the lock text every MCP resource uses (`ACCOUNT_LOCKED_TOOL_ERROR`).
WORKER_REFUSALS: Tuple[TaskRefusal, ...] = (
    TaskRefusal.NOT_FOUND, TaskRefusal.STALE_LEASE, TaskRefusal.LEASE_EXPIRED, TaskRefusal.RENEW_CAP,
    TaskRefusal.ALREADY_SUBMITTED, TaskRefusal.ALREADY_FINAL, TaskRefusal.TOO_LARGE, TaskRefusal.INVALID_TEXT)


def text_key_of_refusal(code: TaskRefusal) -> str:
    """The key of the line a refusal answers with: `E_NOT_FOUND`, `E_STALE_LEASE`, …"""
    return f"E_{code.name}"


REFUSAL_KEYS: Tuple[str, ...] = tuple(text_key_of_refusal(code) for code in WORKER_REFUSALS)

LINE_KEYS: Tuple[str, ...] = (CLAIMED, WRITTEN_BY, TITLE, TASK_TEXT, NO_TASK, LIST_HEAD, LIST_QUEUED, LIST_LEASED,
                              LIST_EMPTY, RENEWED, SUBMITTED, RELEASED, FAILED, ID_REQUIRED, RATE_LIMITED, REFUSED)

REQUIRED_RESULT_KEYS: FrozenSet[str] = frozenset({*LINE_KEYS, *REFUSAL_KEYS})

RESULT_PLACEHOLDERS: Mapping[str, FrozenSet[str]] = MappingProxyType({
    CLAIMED: frozenset({"task_id", "created_at", "lease_id", "lease_until"}),
    TITLE: frozenset({"title"}),
    LIST_HEAD: frozenset({"count"}),
    LIST_QUEUED: frozenset({"task_id", "created_at", "title"}),
    LIST_LEASED: frozenset({"task_id", "created_at", "lease_until", "title"}),
    RENEWED: frozenset({"task_id", "lease_until"}),
    SUBMITTED: frozenset({"task_id", "bytes", "sha256"}),
    RELEASED: frozenset({"task_id"}),
    FAILED: frozenset({"task_id"}),
    ID_REQUIRED: frozenset({"parameter"}),
    RATE_LIMITED: frozenset({"seconds"}),
    **{key: frozenset({"code"}) for key in REFUSAL_KEYS},
    text_key_of_refusal(TaskRefusal.TOO_LARGE): frozenset({"code", "max_bytes"}),
})
