"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3, "Every create is visible").

One pure renderer for the delivery item `create_external_task` returns and `ConversationHandler` posts after the
answer. The task text is appended byte for byte as it was stored, never passed through a template, and never cut: the
owner must see exactly what leaves the system, or what waits for his command, whatever the model said in its answer.
`TASK_TEXT_MAX_CHARS` is bounded so the whole text fits one literal post.

A queued task gets the receipt alone. A held task gets the receipt and the command to paste, returned apart so the
handler can post it as its own message, the easiest thing to copy back.

Public interface:
    EXTERNAL_TASK_RECEIPT_DELIVERY                                   the `DeliveryItem.type`
    receipt_data(created, command) -> dict                           the item's `data` from a `CreatedTask`
    render(data, strings) -> (literal_post, command or None)
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .external_task import HOLD_S, ExternalTaskStatus
from .external_task_views import CreatedTask
from .ui_messages import UIMessage

EXTERNAL_TASK_RECEIPT_DELIVERY = "external_task_receipt"

Strings = Callable[[UIMessage], str]


def receipt_data(created: CreatedTask) -> Dict[str, Any]:
    """`command` is set for a held task only; the raw code appears nowhere else the model can read."""
    held = created.status is ExternalTaskStatus.HELD
    return {
        "status": created.status.value, "worker": created.label, "title": created.title, "text": created.text,
        "task_id": created.task_id,
        "command": f"$task send {created.code}" if held and created.code else None,
    }


def render(data: Mapping[str, Any], strings: Strings) -> Tuple[str, Optional[str]]:
    """The post, and the command alone for a held task. A held receipt without a command is never produced; if the
    data carries none, the post still says the task was not sent."""
    held = data.get("status") == ExternalTaskStatus.HELD.value
    header = strings(UIMessage.EXTERNAL_TASK_HELD if held else UIMessage.EXTERNAL_TASK_HANDED).format(
        worker=data.get("worker") or "?", title=data.get("title") or "", hours=HOLD_S // 3600)
    footer = strings(UIMessage.EXTERNAL_TASK_ID_LINE).format(task_id=data.get("task_id") or "?")
    post = f"{header}\n\n{data.get('text') or ''}\n\n{footer}"
    command = data.get("command")
    return post, (f"`{command}`" if held and command else None)
