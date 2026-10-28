"""
The text of a drive-note receipt and of an erase request (docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md §4.6, §4.8, §4.9).

One pure renderer for every path that posts them — ConversationHandler on a chat turn, UserNotificationService on a
call with Lelik, the `$drive-notes erase` commands — so the paths cannot drift. Input is the DeliveryItem's data
(`NoteReceipt.delivery_data()` / `EraseRequestReceipt.delivery_data()`) and a lookup of localized UI strings
(`LocalizationPort.get_ui_string` bound to the owner's language). The note's own text is appended byte for byte as it
was stored, never passed through a template; classifier values are shown with `_` as a space.

Public interface:
    Strings = Callable[[UIMessage], str]
    render_drive_note_receipt(data, strings) -> str            the `drive_note_receipt` message
    render_drive_note_erase(data, strings) -> tuple[str, str]  the `drive_note_erase` request line, and the command alone
    render_drive_note_erased(erased, left, strings) -> str     the reply to a pasted erase code (S3's command)
    drive_note_call_event(item_type, data) -> str               the user turn of the history pair a call's post appends
    drive_note_late_event(item_type, data) -> str               the same for a late receipt on a chat turn
    RECEIPT_QUOTE_OPEN / RECEIPT_QUOTE_CLOSE                    what surrounds the stored text
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from .drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, ERASE_CODE_TTL, ERASE_SCOPE_ALL, STATUS_ARCHIVED, STATUS_INDEXING_OFF,
    STATUS_NO_CARD, STATUS_NOT_INDEXED, STATUS_OTHER_DRIVE, STATUS_SOON,
)
from .ui_messages import UIMessage

Strings = Callable[[UIMessage], str]

RECEIPT_QUOTE_OPEN = "«"
RECEIPT_QUOTE_CLOSE = "»"

_CALL_VERBS: Dict[str, str] = {"set": "saved", "clear": "removed", "restore": "restored by an undo"}

_STATUS_LINES: Dict[str, UIMessage] = {
    STATUS_SOON: UIMessage.DRIVE_NOTE_STATUS_SOON,
    STATUS_INDEXING_OFF: UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF,
    STATUS_NOT_INDEXED: UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED,
    STATUS_NO_CARD: UIMessage.DRIVE_NOTE_STATUS_NO_CARD,
    STATUS_ARCHIVED: UIMessage.DRIVE_NOTE_STATUS_ARCHIVED,
    STATUS_OTHER_DRIVE: UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE,
}


def _label(value: str) -> str:
    return value.replace("_", " ")


def _place(data: Mapping[str, Any]) -> str:
    """Where the note is: the file's last known path, else its ref (a note with no snapshot)."""
    return str(data.get("path") or data.get("ref") or data.get("note_ref") or "?")


def _stored(data: Mapping[str, Any], strings: Strings) -> List[str]:
    """The stored triple: the text byte for byte in quotes (no empty quote for a classifier-only note), then one line
    of classifiers."""
    lines: List[str] = []
    text = data.get("text") or ""
    if text:
        lines.append(f"{RECEIPT_QUOTE_OPEN}{text}{RECEIPT_QUOTE_CLOSE}")
    parts: List[str] = []
    doc_type = data.get("doc_type") or ""
    if doc_type:
        parts.append(strings(UIMessage.DRIVE_NOTE_TYPE).format(doc_type=_label(doc_type)))
    areas: Sequence[str] = data.get("life_areas") or ()
    if areas:
        parts.append(strings(UIMessage.DRIVE_NOTE_AREAS).format(areas=", ".join(_label(a) for a in areas)))
    if parts:
        lines.append(" · ".join(parts))
    return lines


def _empty(data: Mapping[str, Any]) -> bool:
    return not (data.get("text") or data.get("doc_type") or data.get("life_areas"))


def render_drive_note_receipt(data: Mapping[str, Any], strings: Strings) -> str:
    """The receipt (§4.6): a localized header with the path and version, the stored note, one searchability line on a
    save, and the undo hint. A delete says the history is kept and undo is possible; a no-op says nothing changed."""
    action = data.get("action") or ""
    place = _place(data)
    version = data.get("version") or 0
    noop = bool(data.get("noop"))
    if action == "clear":
        if noop:
            return strings(UIMessage.DRIVE_NOTE_NOTHING_TO_CLEAR).format(path=place)
        return strings(UIMessage.DRIVE_NOTE_CLEARED).format(path=place, version=version)
    if action == "restore":
        if noop:
            header = UIMessage.DRIVE_NOTE_UNDO_REPEATED
        elif _empty(data):
            header = UIMessage.DRIVE_NOTE_RESTORED_EMPTY
        elif data.get("redo"):
            header = UIMessage.DRIVE_NOTE_REDONE
        else:
            header = UIMessage.DRIVE_NOTE_RESTORED
        lines = [strings(header).format(path=place, version=version), *_stored(data, strings)]
        if not noop:
            lines.append(strings(UIMessage.DRIVE_NOTE_UNDO_HINT))
        return "\n".join(lines)
    header = UIMessage.DRIVE_NOTE_UNCHANGED if noop else UIMessage.DRIVE_NOTE_SAVED
    lines = [strings(header).format(path=place, version=version), *_stored(data, strings)]
    status = _STATUS_LINES.get(str(data.get("status") or ""))
    if status is not None:
        lines.append(strings(status))
    if not noop:
        lines.append(strings(UIMessage.DRIVE_NOTE_UNDO_HINT))
    return "\n".join(lines)


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


def render_drive_note_erased(erased: int, left: int, strings: Strings, scope: str = "") -> str:
    """The reply to a pasted erase code: the count erased, or — a partial erase — how many are left (the core's
    CONFLICT refusal detail `erased` / `left` / `scope`): after an erase-all the owner sends the command again (Smart
    cannot erase every note), after one note's erase he asks for it again."""
    if left:
        message = UIMessage.DRIVE_NOTE_ERASE_ALL_PARTIAL if scope == ERASE_SCOPE_ALL else UIMessage.DRIVE_NOTE_ERASE_PARTIAL
        return strings(message).format(erased=erased, left=left)
    return strings(UIMessage.DRIVE_NOTE_ERASED).format(count=erased)


def drive_note_call_event(item_type: str, data: Mapping[str, Any]) -> str:
    """The user turn of the history pair a receipt posted from a call appends (§4.9), so "undo that" later in the chat
    resolves the ref. Never carries the note's text or an erase code."""
    if item_type == DRIVE_NOTE_ERASE_DELIVERY:
        what = "all drive notes" if data.get("scope") == ERASE_SCOPE_ALL else f"the drive note on {_place(data)}"
        return (f"[System: during a call with Lelik, the owner asked to erase the history of {what}; the confirmation "
                "command was posted to the chat]")
    ref = data.get("ref") or data.get("note_ref") or ""
    verb = _CALL_VERBS.get(str(data.get("action") or ""), "saved")
    return (f"[System: drive note {verb} during a call with Lelik: {_place(data)} "
            f"ref={ref} v{data.get('version') or 0}]")


def drive_note_late_event(item_type: str, data: Mapping[str, Any]) -> str:
    """The user turn of the history pair a LATE receipt on a chat turn appends (design Δ10): the write landed after the
    answer was built, so the turn's `drive_note_context` never carried the ref; this line does, in chat wording. Never
    carries the note's text or an erase code."""
    if item_type == DRIVE_NOTE_ERASE_DELIVERY:
        what = "all drive notes" if data.get("scope") == ERASE_SCOPE_ALL else f"the drive note on {_place(data)}"
        return (f"[System: the owner asked to erase the history of {what}; the confirmation command was posted to the "
                "chat after the answer]")
    ref = data.get("ref") or data.get("note_ref") or ""
    verb = _CALL_VERBS.get(str(data.get("action") or ""), "saved")
    return (f"[System: drive note {verb} after the answer was sent: {_place(data)} "
            f"ref={ref} v{data.get('version') or 0}]")


def drive_note_call_posted(item_type: str, data: Mapping[str, Any]) -> str:
    """The model turn of that history pair: a neutral line saying what was posted, never the receipt (final review
    L-I2, C-I1): the consolidator reads a model message's `text`, and an erase cannot scrub chat history, so the note's
    words never enter session history at all."""
    if item_type == DRIVE_NOTE_ERASE_DELIVERY:
        return "[drive note erase request posted to the chat]"
    return f"[drive note receipt posted to the chat: {_place(data)} v{data.get('version') or 0}]"


__all__ = [
    "RECEIPT_QUOTE_CLOSE", "RECEIPT_QUOTE_OPEN", "Strings", "drive_note_call_event", "drive_note_call_posted", "drive_note_late_event",
    "render_drive_note_erase", "render_drive_note_erased", "render_drive_note_receipt",
]
