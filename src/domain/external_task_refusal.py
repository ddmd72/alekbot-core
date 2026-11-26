"""Why the external task queue refused an operation: one closed set of reason codes (EXTERNAL_TASK_QUEUE_RFC §5.2, §5.5).

One module on purpose: the code enum and the exceptions that carry it are read together by every caller.

The surfaces map a code to their answer; this module states the mapping the RFC gives, it does not render anything:
- 401: `key_invalid` (missing, malformed, unknown or revoked key: one answer for all);
- 403: `not_enabled`, `account_locked`, `worker_revoked`;
- 404: `not_found` (a task that is not this caller's, a missing task, a held task seen by a worker: one answer);
- 409: `stale_lease`, `lease_expired`, `renew_cap`, `already_submitted`, `already_final`;
- 413: `too_large`; 400: `invalid_text`;
- owner side: `unknown_worker` (Smart gives it the same line as `not_found`), `hold_not_found` (one neutral line for a
  wrong and an expired code), `open_tasks_cap`;
- registry: `label_invalid`, `label_taken`, `workers_cap`, `keys_cap`, `destination_unavailable`, `wake_unavailable`
  (a server built without the cipher or the outbound guard cannot store a wake-up target). A target the guard refuses
  is not a `TaskRefusal`: it raises `WakeTargetRefused` with its `WakeRefusal` code (`src/domain/wake_refusal.py`);
- task files (§5.12), owner side at create: `files_cap` (more than `MAX_FILES_PER_TASK`), `files_too_large` (over
  `TASK_FILES_MAX_BYTES` together), `files_unavailable` (a server built without the file backend), and
  `file_unresolved`, raised as `TaskFileRefused`, which names the reference and carries the resolver's own line;
- task files, worker side (`get_task_file`): `no_such_file` (404: the task has no file with that number), `file_gone`
  (410: the copy is no longer stored), `link_unavailable` (503: signing failed; the worker asks again). A final task is
  `not_found` and a lapsed lease `lease_expired`, as for the other worker operations.

`contended` and `not_due` never leave the service: a claim candidate another claim took, a timer that fired early.
"""
from enum import Enum
from typing import Optional


class TaskRefusal(str, Enum):
    KEY_INVALID = "key_invalid"
    NOT_ENABLED = "not_enabled"
    ACCOUNT_LOCKED = "account_locked"
    WORKER_REVOKED = "worker_revoked"
    NOT_FOUND = "not_found"
    STALE_LEASE = "stale_lease"
    LEASE_EXPIRED = "lease_expired"
    RENEW_CAP = "renew_cap"
    ALREADY_SUBMITTED = "already_submitted"
    ALREADY_FINAL = "already_final"
    TOO_LARGE = "too_large"
    INVALID_TEXT = "invalid_text"
    UNKNOWN_WORKER = "unknown_worker"
    HOLD_NOT_FOUND = "hold_not_found"
    OPEN_TASKS_CAP = "open_tasks_cap"
    LABEL_INVALID = "label_invalid"
    LABEL_TAKEN = "label_taken"
    WORKERS_CAP = "workers_cap"
    KEYS_CAP = "keys_cap"
    DESTINATION_UNAVAILABLE = "destination_unavailable"
    WAKE_UNAVAILABLE = "wake_unavailable"
    FILES_CAP = "files_cap"
    FILES_TOO_LARGE = "files_too_large"
    FILES_UNAVAILABLE = "files_unavailable"
    FILE_UNRESOLVED = "file_unresolved"
    NO_SUCH_FILE = "no_such_file"
    FILE_GONE = "file_gone"
    LINK_UNAVAILABLE = "link_unavailable"
    CONTENDED = "contended"
    NOT_DUE = "not_due"


class ExternalTaskRefused(Exception):
    """An operation of the queue was refused and wrote nothing; `reason` says why."""

    def __init__(self, reason: TaskRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


class TaskFileRefused(ExternalTaskRefused):
    """A create refused because one attached reference did not resolve (`file_unresolved`): `ref` is the reference as
    given, `line` the resolver's own answer for it when it gave one (a cap, a folder, an email attachment that may not
    leave, a drive that refuses), written for a model; None when the file was not found or could not be read. Nothing
    was stored."""

    def __init__(self, ref: str, line: Optional[str]) -> None:
        super().__init__(TaskRefusal.FILE_UNRESOLVED)
        self.ref = ref
        self.line = line
