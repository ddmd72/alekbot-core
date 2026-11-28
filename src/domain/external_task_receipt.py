"""The literal receipt of an external task create (EXTERNAL_TASK_QUEUE_RFC §5.3, "Every create is visible").

One pure renderer for the delivery item `create_external_task` returns. The receipt is a literal notice
(`domain.literal_notice`, shared with the memory-fact draft and the reminder notices) in a file `task-<id>.txt`: YAML
front matter with the title, the worker's label, the task's note and one `file_<n>` line per attached file (its name and
size; §5.12) (`domain.front_matter`: one quoted line per value, so any note text reads back exactly), a `---` line, then
the task text byte for byte as it was stored, never passed through a template and never cut. The message after the file
carries the header and the task id. The owner must see exactly what leaves the system, and the note the bot will act on
when the result returns, whatever the model said in its answer. The worker receives the title, the text and the
attached files only, never this file.

The header follows the task's status. A task created in a chat turn is `held`: the receipt is the confirmation prompt,
`ConversationHandler` posts it after the answer, and the command to paste is returned apart so the handler can post it
as its own message, the easiest thing to copy back. A task created while one of the owner's reminders runs is `queued`
at once: the receipt is a show-only notice that it was handed over from a reminder (so the owner sees at once when
results keep spawning reminders that spawn tasks), posted by the create itself the moment the task is queued
(`UserNotificationService.post_task_receipt`); it has no command and no button.

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
    receipt_file(data) -> str                                        the file's text: front matter, then the brief
    render(data, strings) -> (LiteralNotice, command)                command None for a queued task
    send_button(data, label) -> Optional[InteractiveMessage]         the command message with the button, from the data
    send_button_message(task_id, code, label) -> InteractiveMessage  the same message, from its parts (the click side)
    send_refusal_reply(reason) -> UIMessage                          the line of a refused confirmation, command or button
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Final, FrozenSet, List, Mapping, Optional, Tuple

from .external_task import HOLD_S, ExternalTaskStatus
from .external_task_files import size_label
from .external_task_refusal import TaskRefusal
from .external_task_views import CreatedTask
from .interactive_binding import InteractiveBinding
from .interactive_message import InteractiveMessage
from .interactive_option import InteractiveOption
from .front_matter import front_matter
from .literal_notice import LiteralNotice, render_literal_notice
from .ui_messages import UIMessage

EXTERNAL_TASK_RECEIPT_DELIVERY = "external_task_receipt"
EXTERNAL_TASK_SEND_KIND: Final[str] = "external_task_send"
SEND_BUTTON_PLATFORMS: Final[FrozenSet[str]] = frozenset({"slack"})
_SEND_COMMAND = "$task send"

Strings = Callable[[UIMessage], str]


def receipt_data(created: CreatedTask) -> Dict[str, Any]:
    """The data's `command` is the confirmation command; the raw code appears nowhere else the model can read. A task
    with files adds `files`: each attached copy's name and size in bytes, in number order (no key for a task without
    files, so its data is what it was before files existed)."""
    data: Dict[str, Any] = {
        "status": created.status.value, "worker": created.label, "title": created.title, "text": created.text,
        "note": created.note, "task_id": created.task_id,
        "command": f"{_SEND_COMMAND} {created.code}" if created.code else None,
    }
    if created.files:
        data["files"] = [{"name": f.name, "size": f.size} for f in created.files]
    return data


def _file_lines(data: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """One front matter line per attached file after the note, `file_<n>: "<name> (<size>)"`, so the confirmation
    covers the files as well as the text (§5.12). Empty for a task without files (the front matter is then unchanged)."""
    files = data.get("files")
    if not isinstance(files, list):
        return []
    lines = []
    for number, entry in enumerate(files, start=1):
        if isinstance(entry, Mapping):
            size = entry.get("size")
            shown = size_label(size) if isinstance(size, int) else "?"
            lines.append((f"file_{number}", f"{entry.get('name') or '?'} ({shown})"))
    return lines


def receipt_file(data: Mapping[str, Any]) -> str:
    """The file's text: the front matter (title, worker, note, then one line per attached file), then the task text
    exactly as stored."""
    head = front_matter([("title", str(data.get("title") or "")), ("worker", str(data.get("worker") or "")),
                         ("note", str(data.get("note") or "")), *_file_lines(data)])
    return head + str(data.get("text") or "")


def render(data: Mapping[str, Any], strings: Strings) -> Tuple[LiteralNotice, Optional[str]]:
    """The notice (the receipt file, then the header and the id line), and the command alone. A queued task (a
    reminder's) gets the handed-over header and no command; any other status the held header, which says the task is
    not sent even when the data carries no command. The file is named by the task id (characters outside
    `[A-Za-z0-9_-]` dropped) and shown under the task's title."""
    task_id = str(data.get("task_id") or "")
    title = str(data.get("title") or "")
    worker = data.get("worker") or "?"
    if data.get("status") == ExternalTaskStatus.QUEUED.value:
        header = strings(UIMessage.EXTERNAL_TASK_QUEUED_FROM_REMINDER).format(worker=worker, title=title)
        command = None
    else:
        header = strings(UIMessage.EXTERNAL_TASK_HELD).format(worker=worker, title=title, hours=HOLD_S // 3600)
        command = data.get("command")
    id_line = strings(UIMessage.EXTERNAL_TASK_ID_LINE).format(task_id=task_id or "?")
    filename = f"task-{re.sub(r'[^A-Za-z0-9_-]', '', task_id) or 'text'}.txt"
    notice = render_literal_notice(header, receipt_file(data), filename=filename, title=title or filename,
                                   lines=[id_line])
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
