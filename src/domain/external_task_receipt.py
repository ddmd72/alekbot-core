"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3, "Every create is visible").

One pure renderer for the delivery item `create_external_task` returns and `ConversationHandler` posts after the
answer. The receipt is a literal notice (`domain.literal_notice`, shared with the memory-fact draft and the reminder
notices): the task text goes byte for byte as it was stored in a file `task-<id>.txt`, never passed through a template
and never cut, and the message after it carries the header and the task id. The owner must see exactly what leaves the
system, or what waits for their command, whatever the model said in its answer.

Every task created in a chat turn waits for the owner's confirmation, so the receipt is always the confirmation
prompt: the receipt, and the command to paste, returned apart so the handler can post it as its own message, the
easiest thing to copy back.

On a platform with buttons (`SEND_BUTTON_PLATFORMS`, Slack) the command message carries one more way to confirm: a
«Send» button (`send_button`). It is an interactive message of the shared mechanism (`InteractiveMessage`): its prompt
is the same command text, its one option is the button. The binding names the task (`EXTERNAL_TASK_SEND_KIND`, the task
id), so the click handler can check whose task it is before anything is done; the option id is the confirmation code,
so the click ends in the same `confirm(user_id, code)` as the pasted command. The code rides in the button only on the
message that already shows it as text. Telegram keeps the command alone.

Public interface:
    EXTERNAL_TASK_RECEIPT_DELIVERY                                   the `DeliveryItem.type`
    EXTERNAL_TASK_SEND_KIND                                          the binding kind of the Send button
    SEND_BUTTON_PLATFORMS                                            where the command message carries the button
    receipt_data(created) -> dict                                    the item's `data` from a `CreatedTask`
    render(data, strings) -> (LiteralNotice, command)
    send_button(data, label) -> Optional[InteractiveMessage]         the command message with the button, from the data
    send_button_message(task_id, code, label) -> InteractiveMessage  the same message, from its parts (the click side)
    send_refusal_reply(reason) -> UIMessage                          the line of a refused confirmation, command or button
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Final, FrozenSet, Mapping, Optional, Tuple

from .external_task import HOLD_S
from .external_task_refusal import TaskRefusal
from .external_task_views import CreatedTask
from .interactive_binding import InteractiveBinding
from .interactive_message import InteractiveMessage
from .interactive_option import InteractiveOption
from .literal_notice import LiteralNotice, render_literal_notice
from .ui_messages import UIMessage

EXTERNAL_TASK_RECEIPT_DELIVERY = "external_task_receipt"
EXTERNAL_TASK_SEND_KIND: Final[str] = "external_task_send"
SEND_BUTTON_PLATFORMS: Final[FrozenSet[str]] = frozenset({"slack"})
_SEND_COMMAND = "$task send"

Strings = Callable[[UIMessage], str]


def receipt_data(created: CreatedTask) -> Dict[str, Any]:
    """The data's `command` is the confirmation command; the raw code appears nowhere else the model can read."""
    return {
        "status": created.status.value, "worker": created.label, "title": created.title, "text": created.text,
        "task_id": created.task_id,
        "command": f"{_SEND_COMMAND} {created.code}" if created.code else None,
    }


def render(data: Mapping[str, Any], strings: Strings) -> Tuple[LiteralNotice, Optional[str]]:
    """The notice (the task text as a file, then the header and the id line), and the command alone. A receipt without
    a command is never produced; if the data carries none, the message still says the task was not sent. The file is
    named by the task id (characters outside `[A-Za-z0-9_-]` dropped) and shown under the task's title."""
    task_id = str(data.get("task_id") or "")
    title = str(data.get("title") or "")
    header = strings(UIMessage.EXTERNAL_TASK_HELD).format(
        worker=data.get("worker") or "?", title=title, hours=HOLD_S // 3600)
    id_line = strings(UIMessage.EXTERNAL_TASK_ID_LINE).format(task_id=task_id or "?")
    filename = f"task-{re.sub(r'[^A-Za-z0-9_-]', '', task_id) or 'text'}.txt"
    notice = render_literal_notice(header, str(data.get("text") or ""), filename=filename, title=title or filename,
                                   lines=[id_line])
    command = data.get("command")
    return notice, (f"`{command}`" if command else None)


def send_button_message(task_id: str, code: str, label: str) -> InteractiveMessage:
    """The command message as an interactive message: the prompt is the command exactly as `render` returns it, the
    one option is the button (`label`) whose id is the code, and the binding names the task. Raises ValueError for a
    task id or a code the mechanism cannot carry."""
    return InteractiveMessage(
        prompt=f"`{_SEND_COMMAND} {code}`",
        options=(InteractiveOption(id=code, label=label),),
        binding=InteractiveBinding(kind=EXTERNAL_TASK_SEND_KIND, id=task_id),
        min_options=1,
    )


def send_button(data: Mapping[str, Any], label: str) -> Optional[InteractiveMessage]:
    """`send_button_message` for a receipt's data; None when the data has no command or no task id (nothing to
    confirm, or nothing to check the owner against)."""
    command, task_id = data.get("command"), data.get("task_id")
    prefix = f"{_SEND_COMMAND} "
    if not isinstance(command, str) or not command.startswith(prefix) or not isinstance(task_id, str) or not task_id:
        return None
    return send_button_message(task_id, command[len(prefix):], label)


def send_refusal_reply(reason: TaskRefusal) -> UIMessage:
    """The one line a refused confirmation gets, from the pasted command and the Send button alike: unavailable when the
    queue is closed to the owner (the flag is off, the account is locked), the neutral not-found line for everything
    else (a wrong, used or expired code)."""
    if reason in (TaskRefusal.NOT_ENABLED, TaskRefusal.ACCOUNT_LOCKED):
        return UIMessage.EXTERNAL_TASK_UNAVAILABLE
    return UIMessage.EXTERNAL_TASK_NOT_FOUND
