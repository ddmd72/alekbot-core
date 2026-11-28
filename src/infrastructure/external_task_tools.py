"""The task specialist's five external task tools — served locally by its DelegationEngine, never via the coordinator
(EXTERNAL_TASK_QUEUE_RFC §5.3; `src/agents/external_task_agent.py`).

Each tool is a declaration builder plus a per-execution handler factory, shaped like `skill_tools.py`. The user id, the
creating turn's origin and the orchestrator's instruction are closed over, never arguments. Every result is a
`Tool note:` or a `Tool error:`. None of the handlers sets `history_context`, so nothing a tool returns is copied into
the persisted turn's metadata.

What the models read and what the owner reads are different channels:
- the result for a create is a tool note with the task id and what happens next (in a chat turn: the task waits for
  the owner's confirmation); the raw confirmation code is NEVER in it. The specialist's model reads it, and so does
  the orchestrator, which gets every result word for word in the specialist's answer;
- the owner's channel: for a chat create, the returned `DeliveryItem`, posted literally after the answer: the receipt
  file (the title, the worker, the task's note, then the task's full text) and the command that confirms it. A
  reminder's create posts the same receipt itself, with no command, as a show-only notice the moment the task is
  queued (`post_receipt`, the way the reminder notices are posted from inside their tool), and returns no item: the
  task is queued and its worker may be woken before the rest of the run, so the owner's record of it never depends on
  how that run ends, and it is posted once.

The brief is the orchestrator's: given the instruction (`commission`), a create whose text is not a part of it, after
whitespace is normalised on both sides, is refused with `ERROR_NOT_THE_BRIEF` and creates nothing. The task's note is
the specialist's own text, written from the instruction, so it is not checked against it. The task's files (§5.12)
are held to the instruction like the brief: every reference in `files` must appear in the instruction (`ERROR_FILE_NOT_NAMED`, naming it), so
the specialist attaches what the orchestrator named and nothing else; a reference the service cannot resolve refuses the
whole create with one tool error naming it (`ERROR_FILE_UNRESOLVED`).

A label that does not exist, a task of another user and a missing task all get the same `Tool error:` line, so nothing
reveals whether a task exists.

Public interface:
    build_create_external_task_declaration(workers, *, entry=CHAT) / build_list_… / build_list_external_workers_… / build_get_… /
    build_cancel_…() -> declaration
    make_create_handler(service, user_id, origin, *, commission=None, entry=CHAT, post_receipt=None) -> LocalToolHandler
    ReceiptPoster                                   posts a reminder create's receipt data; True when it was posted
    make_list_handler / make_list_workers_handler / make_get_handler / make_cancel_handler(service, user_id)
    recorded(handlers, sink) -> handlers           each call's (ToolCall, ToolResult) appended to `sink`, in call order
"""
import asyncio
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.agent import DeliveryItem
from ..domain.external_task import HOLD_S, ExternalTaskStatus, TaskFailureCode, TaskOrigin
from ..domain.external_task_entry import TaskEntry
from ..domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from ..domain.external_task_files import size_label
from ..domain.external_task_refusal import ExternalTaskRefused, TaskFileRefused, TaskRefusal
from ..domain.external_task_text import prepare_task_text, prepare_title
from ..domain.external_task_texts import (
    CANCEL_DESCRIPTION,
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_EXTERNAL_TASK_TOOL,
    ERROR_ARGUMENTS,
    ERROR_BRIEF_TOO_LONG,
    ERROR_FAILED,
    ERROR_FILE_NOT_NAMED,
    ERROR_FILE_UNRESOLVED,
    ERROR_FILES_ARGUMENT,
    ERROR_FILES_CAP,
    ERROR_FILES_TOO_LARGE,
    ERROR_FILES_UNAVAILABLE,
    ERROR_INVALID_TEXT,
    FILE_REASON_NOT_FOUND,
    FILE_REASON_READER,
    ERROR_NOT_FOUND,
    ERROR_NOT_THE_BRIEF,
    ERROR_NOTE_TOO_LONG,
    ERROR_OPEN_CAP,
    ERROR_REFUSED,
    ERROR_STATUS,
    ERROR_TITLE_TOO_LONG,
    ERROR_TRUNCATED,
    ERROR_UNAVAILABLE,
    GET_DESCRIPTION,
    GET_EXTERNAL_TASK_TOOL,
    LIST_DESCRIPTION,
    LIST_EXTERNAL_TASKS_TOOL,
    LIST_EXTERNAL_WORKERS_TOOL,
    LIST_WORKERS_DESCRIPTION,
    NOTE_CANCEL_FINAL,
    NOTE_CANCELLED,
    NOTE_FILE_ITEM,
    NOTE_FILES,
    NOTE_GET_CODE,
    NOTE_GET_HEAD,
    NOTE_GET_NO_RESULT,
    NOTE_GET_REASON,
    NOTE_GET_RESULT,
    NOTE_LIST_EMPTY,
    NOTE_LIST_HEADER,
    NOTE_LIST_LINE,
    NOTE_PENDING,
    NOTE_QUEUED,
    NOTE_WORKERS_HEADER,
    NOTE_WORKERS_NONE,
    create_description,
    FILES_PARAMETER_TEXT,
    NOTE_PARAMETER_TEXT,
    STATUS_PARAMETER_TEXT,
    TASK_ID_PARAMETER_TEXT,
    TEXT_PARAMETER_TEXT,
    TITLE_PARAMETER_TEXT,
    WORKER_FILTER_PARAMETER_TEXT,
    worker_parameter_text,
)
from ..domain.external_worker import ExternalWorker
from ..domain.llm import ToolCall
from ..domain.untrusted_text import EXTERNAL_REMINDER, wrap_untrusted
from ..utils.logger import logger
from .delegation_engine import LocalToolHandler, ToolResult

if TYPE_CHECKING:
    from ..services.external_task_service import ExternalTaskService

ReceiptPoster = Callable[[Mapping[str, Any]], Awaitable[bool]]

_REFUSAL_LINES: Dict[TaskRefusal, str] = {
    TaskRefusal.UNKNOWN_WORKER: ERROR_NOT_FOUND,
    TaskRefusal.NOT_FOUND: ERROR_NOT_FOUND,
    TaskRefusal.NOT_ENABLED: ERROR_UNAVAILABLE,
    TaskRefusal.ACCOUNT_LOCKED: ERROR_UNAVAILABLE,
    TaskRefusal.OPEN_TASKS_CAP: ERROR_OPEN_CAP,
    TaskRefusal.INVALID_TEXT: ERROR_INVALID_TEXT,
    TaskRefusal.FILES_CAP: ERROR_FILES_CAP,
    TaskRefusal.FILES_TOO_LARGE: ERROR_FILES_TOO_LARGE,
    TaskRefusal.FILES_UNAVAILABLE: ERROR_FILES_UNAVAILABLE,
}


def _refused(tool_call: ToolCall, exc: ExternalTaskRefused) -> ToolResult:
    logger.info("[external_task_tools] %s refused: %s", tool_call.name, exc.reason.value)
    return ToolResult(name=tool_call.name, result_str=_REFUSAL_LINES.get(exc.reason, ERROR_REFUSED), failed=True)


def _error(tool_call: ToolCall, line: str) -> ToolResult:
    return ToolResult(name=tool_call.name, result_str=line, failed=True)


def _crashed(tool_call: ToolCall, exc: Exception) -> ToolResult:
    logger.error("[external_task_tools] %s failed: %s", tool_call.name, type(exc).__name__, exc_info=True)
    return _error(tool_call, ERROR_FAILED)


def _args(tool_call: ToolCall) -> Dict[str, Any]:
    return dict(tool_call.args or {})


def _text_arg(args: Dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    return value if isinstance(value, str) else None


def _when(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC")


# --- declarations ------------------------------------------------------------------------------------------------

def build_create_external_task_declaration(workers: Sequence[ExternalWorker], *,
                                           entry: TaskEntry = TaskEntry.CHAT) -> Dict[str, Any]:
    """`workers` is never empty: an empty enum is not a valid schema, so the caller leaves the tool out instead. The
    description follows the run's `entry` (what happens to the task differs by it)."""
    return {
        "name": CREATE_EXTERNAL_TASK_TOOL,
        "description": create_description(entry),
        "parameters": {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "enum": [w.label for w in workers],
                           "description": worker_parameter_text(workers)},
                "title": {"type": "string", "description": TITLE_PARAMETER_TEXT},
                "text": {"type": "string", "description": TEXT_PARAMETER_TEXT},
                "note": {"type": "string", "description": NOTE_PARAMETER_TEXT},
                "files": {"type": "array", "items": {"type": "string"}, "description": FILES_PARAMETER_TEXT},
            },
            "required": ["worker", "title", "text", "note"],
        },
    }


def build_list_external_tasks_declaration() -> Dict[str, Any]:
    return {
        "name": LIST_EXTERNAL_TASKS_TOOL,
        "description": LIST_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": [s.value for s in ExternalTaskStatus],
                           "description": STATUS_PARAMETER_TEXT},
                "worker": {"type": "string", "description": WORKER_FILTER_PARAMETER_TEXT},
            },
        },
    }


def build_list_external_workers_declaration() -> Dict[str, Any]:
    return {"name": LIST_EXTERNAL_WORKERS_TOOL, "description": LIST_WORKERS_DESCRIPTION,
            "parameters": {"type": "object", "properties": {}}}


def build_get_external_task_declaration() -> Dict[str, Any]:
    return {
        "name": GET_EXTERNAL_TASK_TOOL,
        "description": GET_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": TASK_ID_PARAMETER_TEXT}},
            "required": ["task_id"],
        },
    }


def build_cancel_external_task_declaration() -> Dict[str, Any]:
    return {
        "name": CANCEL_EXTERNAL_TASK_TOOL,
        "description": CANCEL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": TASK_ID_PARAMETER_TEXT}},
            "required": ["task_id"],
        },
    }


# --- handlers (built per execution attempt) ---------------------------------------------------------------------

def _normalised(text: str) -> str:
    return " ".join(text.split())


def _is_the_brief(text: str, commission: str) -> bool:
    """The text is a part of the instruction, whitespace aside: the model copied the brief, it did not write one."""
    return _normalised(text) in _normalised(commission)


def _too_large(tool_call: ToolCall, title: str, text: str) -> ToolResult:
    """The service checks the title, then the brief, then the note: the first one it would refuse is the long one."""
    for value, prepare, line in ((title, prepare_title, ERROR_TITLE_TOO_LONG),
                                 (text, prepare_task_text, ERROR_BRIEF_TOO_LONG)):
        try:
            prepare(value)
        except ExternalTaskRefused:
            return _error(tool_call, line)
    return _error(tool_call, ERROR_NOTE_TOO_LONG)


def _files_arg(args: Dict[str, Any]) -> Optional[List[str]]:
    """The `files` argument: absent or null is no file; a list of non-empty strings is kept as given (trimmed); anything
    else is None (a malformed argument)."""
    value = args.get("files")
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    return [v.strip() for v in value]


def _first_unnamed(files: Sequence[str], commission: Optional[str]) -> Optional[str]:
    """The first reference that does not appear in the instruction, whitespace aside (§5.12: the specialist attaches
    what the orchestrator named and nothing else); None when every one does, or when there is no instruction to check
    (the handler tested alone)."""
    if commission is None:
        return None
    instruction = _normalised(commission)
    return next((ref for ref in files if _normalised(ref) not in instruction), None)


def make_create_handler(service: "ExternalTaskService", user_id: str, origin: TaskOrigin, *,
                        commission: Optional[str] = None, entry: TaskEntry = TaskEntry.CHAT,
                        post_receipt: Optional[ReceiptPoster] = None) -> LocalToolHandler:
    """`entry` is set by the agent from the run's context, never by the model. A create in a chat turn always waits for
    the owner's confirmation, whatever the turn read, and returns the receipt item (the caller posts it with the
    command). One in a reminder's run is queued at once; its receipt, a show-only notice with no command, is posted
    through `post_receipt` before the tool returns (shielded, so a run cut while it posts still posts it), and no item
    is returned. A receipt that cannot be posted is logged at error level and never raised: the task is already
    queued. With a `commission` (the orchestrator's instruction), a text that is not a part of it is refused before
    the service is asked; None checks nothing (the handler tested alone). The note is required, like the worker, the
    title and the text."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        args = _args(tool_call)
        if args.get("_parse_error") == "truncated_json":
            return _error(tool_call, ERROR_TRUNCATED)
        worker, title, text, note = (_text_arg(args, k) for k in ("worker", "title", "text", "note"))
        if worker is None or title is None or text is None or note is None:
            return _error(tool_call, ERROR_ARGUMENTS)
        files = _files_arg(args)
        if files is None:
            return _error(tool_call, ERROR_FILES_ARGUMENT)
        if commission is not None and not _is_the_brief(text, commission):
            logger.info("[external_task_tools] create refused: the text is not the brief")
            return _error(tool_call, ERROR_NOT_THE_BRIEF)
        unnamed = _first_unnamed(files, commission)
        if unnamed is not None:
            logger.info("[external_task_tools] create refused: a file reference is not in the instruction")
            return _error(tool_call, ERROR_FILE_NOT_NAMED.format(ref=unnamed))
        try:
            created = await service.create(user_id, worker, title, text, origin, entry=entry, note=note, files=files)
        except TaskFileRefused as exc:
            reason = FILE_REASON_READER.format(line=exc.line) if exc.line else FILE_REASON_NOT_FOUND
            return _error(tool_call, ERROR_FILE_UNRESOLVED.format(ref=exc.ref, reason=reason))
        except ExternalTaskRefused as exc:
            if exc.reason is TaskRefusal.TOO_LARGE:
                return _too_large(tool_call, title, text)
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        if entry is TaskEntry.REMINDER:
            result = NOTE_QUEUED.format(task_id=created.task_id, worker=created.label, title=created.title)
        else:
            result = NOTE_PENDING.format(task_id=created.task_id, worker=created.label, hours=HOLD_S // 3600)
        if created.files:
            result += " " + NOTE_FILES.format(files="; ".join(
                NOTE_FILE_ITEM.format(number=f.number, name=f.name, size=size_label(f.size)) for f in created.files))
        if entry is TaskEntry.REMINDER:
            await _post_reminder_receipt(post_receipt, receipt_data(created), created.task_id)
            return ToolResult(name=tool_call.name, result_str=result)
        return ToolResult(
            name=tool_call.name, result_str=result,
            delivery_items=[DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))],
        )

    return handle


async def _post_reminder_receipt(post_receipt: Optional[ReceiptPoster], data: Mapping[str, Any], task_id: str) -> None:
    """The show-only receipt of a task a reminder's run queued, posted once, here. Never raises; a cancel of the run
    while it posts does not stop the post (shielded)."""
    if post_receipt is None:
        logger.error("[external_task_tools] receipt not posted: no notifier task=%s", task_id)
        return
    try:
        posted = await asyncio.shield(post_receipt(data))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("[external_task_tools] receipt failed task=%s: %s", task_id, type(exc).__name__)
        return
    if not posted:
        logger.error("[external_task_tools] receipt not posted task=%s", task_id)


def make_list_handler(service: "ExternalTaskService", user_id: str) -> LocalToolHandler:

    async def handle(tool_call: ToolCall) -> ToolResult:
        args = _args(tool_call)
        raw_status = args.get("status")
        status: Optional[ExternalTaskStatus] = None
        if raw_status not in (None, ""):
            try:
                status = ExternalTaskStatus(raw_status)
            except ValueError:
                return _error(tool_call, ERROR_STATUS)
        worker = _text_arg(args, "worker") or None
        try:
            tasks = await service.list_tasks(user_id, status, worker)
        except ExternalTaskRefused as exc:
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        if not tasks:
            return ToolResult(name=tool_call.name, result_str=NOTE_LIST_EMPTY)
        lines = [NOTE_LIST_LINE.format(task_id=t.task_id, worker=t.label, title=t.title, status=t.status.value,
                                       created=_when(t.created_at), updated=_when(t.updated_at)) for t in tasks]
        return ToolResult(name=tool_call.name, result_str="\n".join([NOTE_LIST_HEADER, *lines]))

    return handle


def make_list_workers_handler(service: "ExternalTaskService", user_id: str) -> LocalToolHandler:
    async def handle(tool_call: ToolCall) -> ToolResult:
        try:
            workers = await service.list_workers(user_id)
        except ExternalTaskRefused as exc:
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        if not workers:
            return ToolResult(name=tool_call.name, result_str=NOTE_WORKERS_NONE)
        # Labels only (charset-restricted): a description is free text typed in the Cabinet and stays out of every
        # model-facing text.
        lines = [w.label for w in workers]
        return ToolResult(name=tool_call.name, result_str="\n".join([NOTE_WORKERS_HEADER, *lines]))

    return handle


def make_get_handler(service: "ExternalTaskService", user_id: str) -> LocalToolHandler:
    """A result and a failure reason are the worker's text: wrapped as outside content after the reminder."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        task_id = _text_arg(_args(tool_call), "task_id")
        if not task_id:
            return _error(tool_call, ERROR_ARGUMENTS)
        try:
            view = await service.get_task(user_id, task_id)
        except ExternalTaskRefused as exc:
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        summary = view.summary
        lines = [NOTE_GET_HEAD.format(task_id=summary.task_id, worker=summary.label, title=summary.title,
                                      status=summary.status.value)]
        if summary.status is ExternalTaskStatus.DONE:
            lines += _wrapped(view.result, task_id, NOTE_GET_RESULT, NOTE_GET_NO_RESULT)
        elif view.failure_code is TaskFailureCode.WORKER_FAILED:
            lines += _wrapped(view.failure_reason, task_id, NOTE_GET_REASON, NOTE_GET_CODE.format(
                code=TaskFailureCode.WORKER_FAILED.value))
        elif view.failure_code is not None:
            lines.append(NOTE_GET_CODE.format(code=view.failure_code.value))
        return ToolResult(name=tool_call.name, result_str="\n".join(lines))

    return handle


def _wrapped(body: Optional[str], task_id: str, lead: str, absent: str) -> list:
    if not body:
        return [absent]
    wrapped = wrap_untrusted(body, nonce=secrets.token_hex(8), store="workspace", ref=f"external_task:{task_id}",
                             label=f"external task {task_id}")
    return [lead, EXTERNAL_REMINDER, wrapped]


def make_cancel_handler(service: "ExternalTaskService", user_id: str) -> LocalToolHandler:
    async def handle(tool_call: ToolCall) -> ToolResult:
        task_id = _text_arg(_args(tool_call), "task_id")
        if not task_id:
            return _error(tool_call, ERROR_ARGUMENTS)
        try:
            await service.cancel(user_id, task_id)
        except ExternalTaskRefused as exc:
            if exc.reason is TaskRefusal.ALREADY_FINAL:
                return ToolResult(name=tool_call.name, result_str=NOTE_CANCEL_FINAL.format(task_id=task_id))
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        return ToolResult(name=tool_call.name, result_str=NOTE_CANCELLED.format(task_id=task_id))

    return handle


def recorded(handlers: Mapping[str, LocalToolHandler],
             sink: List[Tuple[ToolCall, ToolResult]]) -> Dict[str, LocalToolHandler]:
    """The same handlers, each appending its `(call, result)` to `sink` when it returns: the specialist builds its
    answer and its receipts from `sink`, so a run cut after a create still reports it and posts its receipt."""

    def wrap(handler: LocalToolHandler) -> LocalToolHandler:
        async def handle(tool_call: ToolCall) -> ToolResult:
            result = await handler(tool_call)
            sink.append((tool_call, result))
            return result
        return handle

    return {name: wrap(handler) for name, handler in handlers.items()}
