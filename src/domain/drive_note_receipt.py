"""
STUB (part C's worktree only). Part A owns this module (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §4.6, §4.8, §4.9): the
receipt and erase renderers shared by every path. Only the two functions part C's `$drive-notes` command calls are
stubbed, with A's signatures and bodies; the assembly keeps A's module whole and drops this file.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Tuple

from .drive_note import ERASE_CODE_TTL, ERASE_SCOPE_ALL
from .ui_messages import UIMessage

Strings = Callable[[UIMessage], str]


def _place(data: Mapping[str, Any]) -> str:
    """Where the note is: the file's last known path, else its ref (a note with no snapshot)."""
    return str(data.get("path") or data.get("ref") or data.get("note_ref") or "?")


def render_drive_note_erase(data: Mapping[str, Any], strings: Strings) -> Tuple[str, str]:
    """An erase request (§4.8): the line with the path and the version count (or the note count, for erase-all), and
    the command alone as a second message, the easiest thing to copy and paste back."""
    minutes = int(ERASE_CODE_TTL.total_seconds() // 60)
    if data.get("scope") == ERASE_SCOPE_ALL:
        line = strings(UIMessage.DRIVE_NOTE_ERASE_ALL_REQUEST).format(count=data.get("note_count") or 0,
                                                                     minutes=minutes)
    else:
        line = strings(UIMessage.DRIVE_NOTE_ERASE_REQUEST).format(path=_place(data),
                                                                 versions=data.get("version_count") or 0,
                                                                 minutes=minutes)
    return line, f"`{data.get('command') or ''}`"


def render_drive_note_erased(erased: int, left: int, strings: Strings) -> str:
    """The reply to a pasted erase code: the count erased, or — a partial erase — how many are left to ask again for
    (the core's CONFLICT refusal detail `erased` / `left`)."""
    if left:
        return strings(UIMessage.DRIVE_NOTE_ERASE_PARTIAL).format(erased=erased, left=left)
    return strings(UIMessage.DRIVE_NOTE_ERASED).format(count=erased)
