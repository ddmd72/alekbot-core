"""The task specialist's five external task tools — served locally by its DelegationEngine, never via the coordinator
(EXTERNAL_TASK_QUEUE_RFC §5.3; `src/agents/external_task_agent.py`).

Each tool is a declaration builder plus a per-execution handler factory, shaped like `skill_tools.py`. The user id, the
creating turn's origin and the orchestrator's instruction are closed over, never arguments. Every result is a
`Tool note:` or a `Tool error:`. None of the handlers sets `history_context`, so nothing a tool returns is copied into
the persisted turn's metadata.

What the models read and what the owner reads are different channels:
- the result for a create is a note with the task id and that the task waits for the owner's confirmation; the raw
  confirmation code is NEVER in it. The specialist's model reads it, and so does the orchestrator, which gets every
  result word for word in the specialist's answer;
- the owner's channel is the returned `DeliveryItem`, posted literally after the answer with the task's full text
  and the command that confirms it.

The brief is the orchestrator's: given the instruction (`commission`), a create whose text is not a part of it, after
whitespace is normalised on both sides, is refused with `ERROR_NOT_THE_BRIEF` and creates nothing.

A label that does not exist, a task of another user and a missing task all get the same `Tool error:` line, so nothing
reveals whether a task exists.

Public interface:
    build_create_external_task_declaration(workers, *, entry=CHAT) / build_list_… / build_list_external_workers_… / build_get_… /
    build_cancel_…() -> declaration
    make_create_handler(service, user_id, origin, *, commission=None, entry=CHAT) -> LocalToolHandler
    make_list_handler / make_list_workers_handler / make_get_handler / make_cancel_handler(service, user_id)
    recorded(handlers, sink) -> handlers           each call's (ToolCall, ToolResult) appended to `sink`, in call order
"""
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.agent import DeliveryItem
from ..domain.external_task import HOLD_S, ExternalTaskStatus, TaskFailureCode, TaskOrigin
from ..domain.external_task_entry import TaskEntry
from ..domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_task_text import prepare_title
from ..domain.external_task_texts import (
    CANCEL_DESCRIPTION,
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_EXTERNAL_TASK_TOOL,
    ERROR_ARGUMENTS,
    ERROR_BRIEF_TOO_LONG,
    ERROR_FAILED,
    ERROR_INVALID_TEXT,
    ERROR_NOT_FOUND,
    ERROR_NOT_THE_BRIEF,
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

_REFUSAL_LINES: Dict[TaskRefusal, str] = {
    TaskRefusal.UNKNOWN_WORKER: ERROR_NOT_FOUND,
    TaskRefusal.NOT_FOUND: ERROR_NOT_FOUND,
    TaskRefusal.NOT_ENABLED: ERROR_UNAVAILABLE,
    TaskRefusal.ACCOUNT_LOCKED: ERROR_UNAVAILABLE,
    TaskRefusal.OPEN_TASKS_CAP: ERROR_OPEN_CAP,
    TaskRefusal.INVALID_TEXT: ERROR_INVALID_TEXT,
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
            },
            "required": ["worker", "title", "text"],
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


def _too_large(tool_call: ToolCall, title: str) -> ToolResult:
    """The service checks the title first: a title it accepts means the brief is the long one."""
    try:
        prepare_title(title)
    except ExternalTaskRefused:
        return _error(tool_call, ERROR_TITLE_TOO_LONG)
    return _error(tool_call, ERROR_BRIEF_TOO_LONG)


def make_create_handler(service: "ExternalTaskService", user_id: str, origin: TaskOrigin, *,
                        commission: Optional[str] = None, entry: TaskEntry = TaskEntry.CHAT) -> LocalToolHandler:
    """`entry` is set by the agent from the run's context, never by the model. A create in a chat turn always waits for
    the owner's confirmation, whatever the turn read; one in a reminder's run is queued at once, with no receipt. With a `commission`
    (the orchestrator's instruction), a text that is not a part of it is refused before the service is asked; None
    checks nothing (the handler tested alone)."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        args = _args(tool_call)
        if args.get("_parse_error") == "truncated_json":
            return _error(tool_call, ERROR_TRUNCATED)
        worker, title, text = (_text_arg(args, k) for k in ("worker", "title", "text"))
        if worker is None or title is None or text is None:
            return _error(tool_call, ERROR_ARGUMENTS)
        if commission is not None and not _is_the_brief(text, commission):
            logger.info("[external_task_tools] create refused: the text is not the brief")
            return _error(tool_call, ERROR_NOT_THE_BRIEF)
        try:
            created = await service.create(user_id, worker, title, text, origin, entry=entry)
        except ExternalTaskRefused as exc:
            if exc.reason is TaskRefusal.TOO_LARGE:
                return _too_large(tool_call, title)
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        if entry is TaskEntry.REMINDER:
            return ToolResult(name=tool_call.name,
                              result_str=NOTE_QUEUED.format(task_id=created.task_id, worker=created.label,
                                                            title=created.title))
        note = NOTE_PENDING.format(task_id=created.task_id, worker=created.label, hours=HOLD_S // 3600)
        return ToolResult(
            name=tool_call.name, result_str=note,
            delivery_items=[DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))],
        )

    return handle


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
