"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3, "Every create is visible").

One pure renderer for the delivery item `create_external_task` returns and `ConversationHandler` posts after the
answer. The task text is appended byte for byte as it was stored, never passed through a template, and never cut: the
owner must see exactly what leaves the system, or what waits for their command, whatever the model said in its answer.
`TASK_TEXT_MAX_CHARS` is bounded so the whole text fits one literal post.

Every task created in a chat turn waits for the owner's confirmation, so the receipt is always the confirmation
prompt: the receipt, and the command to paste, returned apart so the handler can post it as its own message, the
easiest thing to copy back.

Public interface:
    EXTERNAL_TASK_RECEIPT_DELIVERY                                   the `DeliveryItem.type`
    receipt_data(created) -> dict                                    the item's `data` from a `CreatedTask`
    render(data, strings) -> (literal_post, command)
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .external_task import HOLD_S
from .external_task_views import CreatedTask
from .ui_messages import UIMessage

EXTERNAL_TASK_RECEIPT_DELIVERY = "external_task_receipt"

Strings = Callable[[UIMessage], str]


def receipt_data(created: CreatedTask) -> Dict[str, Any]:
    """The data's `command` is the confirmation command; the raw code appears nowhere else the model can read."""
    return {
        "status": created.status.value, "worker": created.label, "title": created.title, "text": created.text,
        "task_id": created.task_id,
        "command": f"$task send {created.code}" if created.code else None,
    }


def render(data: Mapping[str, Any], strings: Strings) -> Tuple[str, Optional[str]]:
    """The post, and the command alone. A receipt without a command is never produced; if the data carries none, the
    post still says the task was not sent."""
    header = strings(UIMessage.EXTERNAL_TASK_HELD).format(
        worker=data.get("worker") or "?", title=data.get("title") or "", hours=HOLD_S // 3600)
    footer = strings(UIMessage.EXTERNAL_TASK_ID_LINE).format(task_id=data.get("task_id") or "?")
    post = f"{header}\n\n{data.get('text') or ''}\n\n{footer}"
    command = data.get("command")
    return post, (f"`{command}`" if command else None)
