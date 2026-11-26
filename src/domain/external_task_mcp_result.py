"""The worker MCP resource's tool results: one outcome of `ExternalTaskService`, or one refusal, as one text
(EXTERNAL_TASK_QUEUE_RFC §5.4).

Every word comes from the result texts (`MCP_TASK_RESULT_TEXTS`, keys in `external_task_mcp_contract`); this module
only picks the keys, fills their placeholders and orders the lines. The one text it does not take from the tokens is
the account lock's, `ACCOUNT_LOCKED_TOOL_ERROR`, which every MCP resource answers alike.

The claim puts code-written provenance first (`CLAIMED`: task id, created time, lease id and end; then `WRITTEN_BY`),
then the title, then the task's files when it has any (`FILES_HEAD`, one `FILE_LINE` each, never a link), then
`TASK_TEXT` and the task text as written, from the next line to the end: the owner's brief, plain.

`is_error` is MCP's "the call did not do its operation": every refusal, an empty id and a rate limit. A claim that found
no task did its operation and is not an error. `status` names the answer for the span and the log line only (a code,
never a text). Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from . import external_task_mcp_contract as contract
from .external_task import RESULT_MAX_BYTES, ExternalTaskStatus
from .external_task_refusal import TaskRefusal
from .external_task_views import ClaimedTask, LeaseGrant, SubmitReceipt, TaskFileLink, WorkerTaskSummary
from .mcp import ACCOUNT_LOCKED_TOOL_ERROR

OK = "ok"
NO_TASK = "no_task"
ID_REQUIRED = "id_required"
RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class TaskToolAnswer:
    text: str
    is_error: bool
    status: str


def _when(moment: datetime) -> str:
    return moment.isoformat()


def _ok(text: str, status: str = OK) -> TaskToolAnswer:
    return TaskToolAnswer(text=text, is_error=False, status=status)


def claimed(results: Mapping[str, str], task: Optional[ClaimedTask]) -> TaskToolAnswer:
    """A claim's answer: the claimed task in provenance-then-content order, or `NO_TASK`. A task with files lists them
    after the title, one line each (number, name, type, size), with no link: `get_task_file` gives one (§5.12)."""
    if task is None:
        return _ok(results[contract.NO_TASK], NO_TASK)
    head = results[contract.CLAIMED].format(task_id=task.task_id, created_at=_when(task.created_at),
                                            lease_id=task.lease_id, lease_until=_when(task.lease_until))
    lines = [head, results[contract.WRITTEN_BY], results[contract.TITLE].format(title=task.title)]
    if task.files:
        lines.append(results[contract.FILES_HEAD].format(count=len(task.files)))
        lines += [results[contract.FILE_LINE].format(number=f.number, name=f.name, mime=f.mime, size=f.size)
                  for f in task.files]
    lines += [results[contract.TASK_TEXT], task.text]
    return _ok("\n".join(lines))


def file_link(results: Mapping[str, str], link: TaskFileLink) -> TaskToolAnswer:
    """`get_task_file`'s answer: the fresh signed link, how long it is valid, and the file it opens."""
    return _ok(results[contract.FILE_LINK].format(task_id=link.task_id, number=link.number, name=link.name,
                                                  mime=link.mime, size=link.size, url=link.url,
                                                  seconds=link.ttl_seconds))


def listed(results: Mapping[str, str], tasks: Sequence[WorkerTaskSummary]) -> TaskToolAnswer:
    """This worker's queued and leased tasks, one line each in the service's order, or `LIST_EMPTY`."""
    if not tasks:
        return _ok(results[contract.LIST_EMPTY])
    lines = [results[contract.LIST_HEAD].format(count=len(tasks))]
    for task in tasks:
        if task.status is ExternalTaskStatus.LEASED and task.lease_until is not None:
            lines.append(results[contract.LIST_LEASED].format(
                task_id=task.task_id, created_at=_when(task.created_at), lease_until=_when(task.lease_until),
                title=task.title))
        else:
            lines.append(results[contract.LIST_QUEUED].format(
                task_id=task.task_id, created_at=_when(task.created_at), title=task.title))
    return _ok("\n".join(lines))


def renewed(results: Mapping[str, str], grant: LeaseGrant) -> TaskToolAnswer:
    return _ok(results[contract.RENEWED].format(task_id=grant.task_id, lease_until=_when(grant.lease_until)))


def submitted(results: Mapping[str, str], receipt: SubmitReceipt) -> TaskToolAnswer:
    """The same answer for an accepted result and for its replay."""
    return _ok(results[contract.SUBMITTED].format(task_id=receipt.task_id, bytes=receipt.bytes,
                                                  sha256=receipt.sha256))


def released(results: Mapping[str, str], task_id: str) -> TaskToolAnswer:
    return _ok(results[contract.RELEASED].format(task_id=task_id))


def failed(results: Mapping[str, str], task_id: str) -> TaskToolAnswer:
    return _ok(results[contract.FAILED].format(task_id=task_id))


def id_required(results: Mapping[str, str], parameter: str) -> TaskToolAnswer:
    """An empty `task_id` or `lease_id`: a line the caller can correct, not a protocol error."""
    return TaskToolAnswer(text=results[contract.ID_REQUIRED].format(parameter=parameter), is_error=True,
                          status=ID_REQUIRED)


def rate_limited(results: Mapping[str, str], seconds: int) -> TaskToolAnswer:
    return TaskToolAnswer(text=results[contract.RATE_LIMITED].format(seconds=seconds), is_error=True,
                          status=RATE_LIMITED)


def refused(results: Mapping[str, str], reason: TaskRefusal) -> TaskToolAnswer:
    """A refusal's line: its own for a worker refusal, the lock text for `account_locked`, `REFUSED` (neutral, no
    code) for `not_enabled`, `worker_revoked` and any other code."""
    if reason is TaskRefusal.ACCOUNT_LOCKED:
        text = ACCOUNT_LOCKED_TOOL_ERROR
    elif reason in contract.WORKER_REFUSALS:
        text = results[contract.text_key_of_refusal(reason)].format(code=reason.value, max_bytes=RESULT_MAX_BYTES)
    else:
        text = results[contract.REFUSED]
    return TaskToolAnswer(text=text, is_error=True, status=reason.value)
