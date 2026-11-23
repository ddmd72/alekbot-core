"""Smart's four external task tools — served locally by the DelegationEngine, never via the coordinator
(EXTERNAL_TASK_QUEUE_RFC §5.3).

Each tool is a declaration builder plus a per-execution handler factory, shaped like `skill_tools.py`. The user id and
the creating turn's origin are closed over, never arguments. Every result is a `Tool note:` or a `Tool error:`. None
of the handlers sets `history_context`, so nothing a tool returns is copied into the persisted turn's metadata.

What the model reads and what the owner reads are different channels:
- the model's result for a create is a note with the task id (and, for a held task, that it waits for the owner's
  command); the raw confirmation code is NEVER in it;
- the owner's channel is the returned `DeliveryItem`, posted literally after the answer with the task's full text
  (and, for a held task, the command).

A label that does not exist, a task of another user and a missing task all get the same `Tool error:` line, so nothing
reveals whether a task exists.

Public interface:
    build_create_external_task_declaration(workers) / build_list_… / build_get_… / build_cancel_…() -> declaration
    make_create_handler(service, user_id, origin, tracker) -> LocalToolHandler
    make_list_handler(service, user_id, tracker) / make_list_workers_handler(service, user_id) / make_get_handler(service, user_id, tracker) / make_cancel_handler(service, user_id)
"""
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional, Sequence

from ..domain.agent import DeliveryItem
from ..domain.external_task import ExternalTaskStatus, TaskFailureCode, TaskOrigin
from ..domain.external_task_receipt import EXTERNAL_TASK_RECEIPT_DELIVERY, receipt_data
from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_task_texts import (
    CANCEL_DESCRIPTION,
    CANCEL_EXTERNAL_TASK_TOOL,
    CREATE_DESCRIPTION,
    CREATE_EXTERNAL_TASK_TOOL,
    ERROR_ARGUMENTS,
    ERROR_FAILED,
    ERROR_INVALID_TEXT,
    ERROR_NOT_FOUND,
    ERROR_OPEN_CAP,
    ERROR_REFUSED,
    ERROR_STATUS,
    ERROR_TOO_LARGE,
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
    NOTE_HELD,
    NOTE_LIST_EMPTY,
    NOTE_LIST_HEADER,
    NOTE_LIST_LINE,
    NOTE_QUEUED,
    NOTE_WORKERS_HEADER,
    NOTE_WORKERS_NONE,
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
from .taint_tracker import TaintTracker

if TYPE_CHECKING:
    from ..services.external_task_service import ExternalTaskService

_REFUSAL_LINES: Dict[TaskRefusal, str] = {
    TaskRefusal.UNKNOWN_WORKER: ERROR_NOT_FOUND,
    TaskRefusal.NOT_FOUND: ERROR_NOT_FOUND,
    TaskRefusal.NOT_ENABLED: ERROR_UNAVAILABLE,
    TaskRefusal.ACCOUNT_LOCKED: ERROR_UNAVAILABLE,
    TaskRefusal.OPEN_TASKS_CAP: ERROR_OPEN_CAP,
    TaskRefusal.TOO_LARGE: ERROR_TOO_LARGE,
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

def build_create_external_task_declaration(workers: Sequence[ExternalWorker]) -> Dict[str, Any]:
    """`workers` is never empty: an empty enum is not a valid schema, so the caller leaves the tool out instead."""
    return {
        "name": CREATE_EXTERNAL_TASK_TOOL,
        "description": CREATE_DESCRIPTION,
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

def make_create_handler(service: "ExternalTaskService", user_id: str, origin: TaskOrigin,
                        tracker: TaintTracker) -> LocalToolHandler:
    """`tracker.tainted` is read at call time: a create after the execution read outside content is held."""

    async def handle(tool_call: ToolCall) -> ToolResult:
        args = _args(tool_call)
        if args.get("_parse_error") == "truncated_json":
            return _error(tool_call, ERROR_TRUNCATED)
        worker, title, text = (_text_arg(args, k) for k in ("worker", "title", "text"))
        if worker is None or title is None or text is None:
            return _error(tool_call, ERROR_ARGUMENTS)
        try:
            created = await service.create(user_id, worker, title, text, origin, tainted=tracker.tainted)
        except ExternalTaskRefused as exc:
            return _refused(tool_call, exc)
        except Exception as exc:
            return _crashed(tool_call, exc)
        held = created.status is ExternalTaskStatus.HELD
        note = (NOTE_HELD if held else NOTE_QUEUED).format(task_id=created.task_id, worker=created.label)
        return ToolResult(
            name=tool_call.name, result_str=note,
            delivery_items=[DeliveryItem(type=EXTERNAL_TASK_RECEIPT_DELIVERY, data=receipt_data(created))],
        )

    return handle


def make_list_handler(service: "ExternalTaskService", user_id: str, tracker: TaintTracker) -> LocalToolHandler:
    """A listed title may have been written in a tainted execution: listing one marks this execution tainted."""

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
        if any(t.created_tainted for t in tasks):
            tracker.mark()
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
        lines = [f"{w.label}: {w.description or 'no description'}" for w in workers]
        return ToolResult(name=tool_call.name, result_str="\n".join([NOTE_WORKERS_HEADER, *lines]))

    return handle


def make_get_handler(service: "ExternalTaskService", user_id: str, tracker: TaintTracker) -> LocalToolHandler:
    """A result and a failure reason are the worker's text: wrapped as outside content after the reminder, and the
    execution is marked tainted (the engine's observer marks it too: this tool is off the allowlist)."""

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
        if view.untrusted:
            tracker.mark()
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
