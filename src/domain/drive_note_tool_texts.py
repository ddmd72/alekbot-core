"""The drive specialist's note tools: names, descriptions, declarations and the lines their results carry
(DRIVE_SPECIALIST_RFC §9.1, §9.2; design §4.3, Δ5, Δ8, Δ10). Pure: domain modules only.

Eleven one-purpose tools, CRUD-shaped: no tool replaces more than its name says, and an empty or omitted field never
removes or changes anything (an empty field is "not given"; a write that would leave a note empty is refused, Δ5). Flat
string parameters (`life_areas` a string array); no `enum`, `pattern`, `maxItems` or `additionalProperties`: limits are
stated in the descriptions and enforced in code. Every tool takes `item_ref` (never `file_ref` or `name`): a `drive:`
ref, or the `drive_note:` ref a deleted-files result shows.

No result line carries the note's words (Δ8) except `get_note`'s, which wraps them as untrusted data: the owner sees
the stored note in a receipt the system posts, and session history gets only `[Drive note: <path> ref=… v<n>]`.

Public interface:
    GET_NOTE … ERASE_NOTE, NOTE_TOOLS, NOTE_WRITE_TOOLS           the names
    drive_note_tool_declarations(*, writes) -> list                get_note, then the ten writers when `writes`
    note_receipt_line(receipt) -> str                              a write's op line (no note words)
    erase_request_line(request) -> str                             an erase request's op line (never the code)
    note_view_text(view, nonce) -> str                             get_note's block
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .drive_card import owner_note_line
from .drive_labels import DOC_TYPES, LIFE_DOMAINS
from .drive_note import (
    LIFE_AREAS_MAX, NOTE_CHARS_MAX, NOTE_WORDS_MAX, STATUS_ARCHIVED, STATUS_INDEXING_OFF, STATUS_NO_CARD,
    STATUS_NOT_INDEXED, STATUS_OTHER_DRIVE, STATUS_SOON, EraseRequestReceipt, NoteReceipt,
)
from .drive_note_view import NoteView
from .drive_search import wrap_untrusted

GET_NOTE = "get_note"
CREATE_NOTE = "create_note"
ADD_TO_NOTE = "add_to_note"
REPLACE_NOTE_TEXT = "replace_note_text"
SET_NOTE_TYPE = "set_note_type"
CLEAR_NOTE_TYPE = "clear_note_type"
ADD_LIFE_AREAS = "add_life_areas"
REMOVE_LIFE_AREAS = "remove_life_areas"
DELETE_NOTE = "delete_note"
UNDO_NOTE = "undo_note"
ERASE_NOTE = "erase_note"

NOTE_WRITE_TOOLS: Tuple[str, ...] = (CREATE_NOTE, ADD_TO_NOTE, REPLACE_NOTE_TEXT, SET_NOTE_TYPE, CLEAR_NOTE_TYPE,
                                     ADD_LIFE_AREAS, REMOVE_LIFE_AREAS, DELETE_NOTE, UNDO_NOTE, ERASE_NOTE)
NOTE_TOOLS: Tuple[str, ...] = (GET_NOTE, *NOTE_WRITE_TOOLS)

# -- parameter descriptions -----------------------------------------------------------------------------------------
_CLASSIFIER_RULE = ("only what the owner named or confirmed in this conversation, as the commission states it; never "
                    "from the file, a card or your reading; empty otherwise")
_LENGTH = f"At most {NOTE_WORDS_MAX} words and {NOTE_CHARS_MAX} characters"

ITEM_REF = ("The drive: ref of the file or folder, from a card, a listing, a [Drive: …] label, the commission or "
            "the state block; or the drive_note:<id> ref a deleted-files result shows for a note on a deleted file.")
CREATE_TEXT = (f"Optional: the owner's words about the item, in his language: what it is, who or what it relates to. "
               f"{_LENGTH}. Empty when the owner gave only a type or life areas.")
ADD_TEXT = (f"The owner's words to add at the end of the note's text, in his language. The whole text stays within "
            f"{_LENGTH.lower()}.")
REPLACE_TEXT = (f"The note's whole new text: the owner's words, in his language. {_LENGTH}. Never empty: to remove the "
                "note use delete_note.")
DOC_TYPE = f"Optional: the document type, one of {', '.join(DOC_TYPES)}: {_CLASSIFIER_RULE}."
SET_DOC_TYPE = f"The document type, one of {', '.join(DOC_TYPES)}: {_CLASSIFIER_RULE}."
LIFE_AREAS = (f"Optional: up to {LIFE_AREAS_MAX} life areas, of {', '.join(LIFE_DOMAINS)}: {_CLASSIFIER_RULE}.")
ADD_AREAS = (f"The life areas to add, of {', '.join(LIFE_DOMAINS)}; the note holds at most {LIFE_AREAS_MAX} in "
             f"total: {_CLASSIFIER_RULE}.")
REMOVE_AREAS = f"The life areas to remove, of {', '.join(LIFE_DOMAINS)}; one the note does not hold is ignored."

# -- what a call's result says (op lines, the specialist's tool results) ----------------------------------------------
NOTE_STILL_RUNNING = ("The note change was started and finishes on its own; its receipt reaches the owner if it is "
                      "stored.")
NOTHING_STORED = "Nothing was stored."
READ_FIRST = ("Read the note with get_note first: replace_note_text replaces only a note read earlier in this "
              "request. Nothing was stored.")
NO_NOTE_READ = "There is no note on {ref}."
NOT_OWNER_TURN = ("Notes are written only while the owner is in the conversation; report the request as not done. "
                  "Nothing was stored.")
WRITE_CAP = ("At most {cap} note changes per request; this one was not made. Tell the owner which notes are left to "
             "do.")
TRUNCATED = ("Your arguments were cut off at the output limit; nothing was stored. Call again with a shorter note.")
EMPTY_CREATE = ("There is nothing to write: give the note's text, its type or its life areas. Nothing was stored.")
STORE_DOWN = "The note store is unavailable right now; nothing was changed. Try again in a moment."
UNEXPECTED = "The note could not be stored right now. Try again later."
ITEM_REF_REQUIRED = "the file or folder's drive: ref, or a drive_note: ref"

STATUS_SENTENCES: Dict[str, str] = {
    STATUS_SOON: "Search shows it within a few minutes.",
    STATUS_INDEXING_OFF: "Drive search is off for the owner, so search shows it only once indexing is turned on.",
    STATUS_NOT_INDEXED: ("The file is not in the search index (inside a folder the index keeps as one card, or not "
                         "seen yet), so search does not show the note."),
    STATUS_NO_CARD: "The file has no search card right now, so search does not show the note yet.",
    STATUS_ARCHIVED: "The file is gone from the drive: only a search of deleted files finds this note.",
    STATUS_OTHER_DRIVE: ("Its drive isn't connected: search does not show this note until that drive is connected "
                         "again."),
}


def _place(path: str, ref: str, note_ref: str) -> str:
    return path or ref or note_ref or "the file"


def note_receipt_line(receipt: NoteReceipt) -> str:
    """The op line of a stored, cleared or restored note: the file, the version and the note ref, never the note's
    words (Δ8); the receipt the system posts carries them."""
    place = _place(receipt.path, receipt.ref, receipt.note_ref)
    refs = f" Its note ref: {receipt.note_ref}." if receipt.note_ref else ""
    if receipt.action == "clear":
        if receipt.noop:
            return f"There was no note on {place}; nothing was changed."
        return (f"Note removed from {place}; its history is kept and undo can bring it back.{refs} The system posts a "
                "receipt to the owner.")
    if receipt.action == "restore":
        if receipt.noop:
            return f"The last change of the note on {place} was already undone in this turn; nothing was written."
        if not (receipt.text or receipt.doc_type or receipt.life_areas):
            return (f"Undone: there was no note on {place} before, so the note is removed (version "
                    f"{receipt.version}).{refs} The system posts a receipt to the owner.")
        done = "Redone: the undone change is back" if receipt.redo else "Undone: the previous note is back"
        return (f"{done} on {place} (version {receipt.version}).{refs} The system posts the note word for word: do not "
                "repeat it.")
    if receipt.noop:
        return (f"The note on {place} already says exactly this (version {receipt.version}); nothing was written."
                f"{refs} The system posts it word for word: do not repeat it.")
    return (f"Note saved on {place} (version {receipt.version}).{refs} The system posts the stored note word for word: "
            f"do not repeat it. {STATUS_SENTENCES.get(receipt.status, '')}").rstrip()


def erase_request_line(request: EraseRequestReceipt) -> str:
    """The op line of an erase request: the owner got a code; the line never carries it (§9.1)."""
    return (f"The owner got a code to erase the history of the note on {request.path or 'the file'} "
            f"({request.version_count} versions) for good; nothing is erased until he pastes it in the chat. Your part "
            "is done; you do not know the code.")


def note_view_text(view: NoteView, nonce: str) -> str:
    """get_note's block: the note as a card shows it (`noted by the owner: …`), inside a nonce-tagged wrapper, with
    its refs and version; a cleared note says that undo_note brings it back."""
    place = view.path or view.ref or view.note_ref
    refs = ", ".join(r for r in (view.ref, f"note ref {view.note_ref}") if r)
    if view.cleared:
        return (f"The owner's note on {place} ({refs}) was deleted (version {view.version}); undo_note brings it "
                "back.")
    head = f"The owner's note on {place} ({refs}, version {view.version})"
    if view.archived:
        head += ": the file is gone from the drive; change this note by its note ref"
    body = owner_note_line(view.text, view.doc_type, view.life_areas)
    return f"{head}:\n{wrap_untrusted(body, nonce=nonce, ref=view.ref or view.note_ref, path=view.path)}"


# -- declarations -----------------------------------------------------------------------------------------------------

def _string(description: str) -> Dict[str, str]:
    return {"type": "string", "description": description}


def _areas(description: str) -> Dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _tool(name: str, description: str, required: List[str], **params: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": dict(params), "required": required}}


def drive_note_tool_declarations(*, writes: bool) -> List[Dict[str, Any]]:
    """`get_note`, then, when `writes` (an owner turn, §9.1), the ten writers; a fixed order (Δ16)."""
    ref = _string(ITEM_REF)
    tools = [_tool(GET_NOTE, "Read the owner's note on a file or folder.", ["item_ref"], item_ref=ref)]
    if not writes:
        return tools
    return tools + [
        _tool(CREATE_NOTE, "Add the owner's note to a file or folder that has none.", ["item_ref"], item_ref=ref,
              text=_string(CREATE_TEXT), doc_type=_string(DOC_TYPE), life_areas=_areas(LIFE_AREAS)),
        _tool(ADD_TO_NOTE, "Add the owner's words at the end of the note's text; the rest of the note is kept. On a "
              "file with no note, this creates one.", ["item_ref", "text"], item_ref=ref, text=_string(ADD_TEXT)),
        _tool(REPLACE_NOTE_TEXT, "Replace the whole text of the note. Refused unless get_note read this note earlier "
              "in this run, and refused if the note changed since that read.", ["item_ref", "text"], item_ref=ref,
              text=_string(REPLACE_TEXT)),
        _tool(SET_NOTE_TYPE, "Set the document type of the note. On a file with no note, this creates one holding "
              "only the type.", ["item_ref", "doc_type"], item_ref=ref, doc_type=_string(SET_DOC_TYPE)),
        _tool(CLEAR_NOTE_TYPE, "Remove the document type from the note.", ["item_ref"], item_ref=ref),
        _tool(ADD_LIFE_AREAS, "Add life areas to the note; the others are kept. On a file with no note, this creates "
              "one holding only the areas.", ["item_ref", "life_areas"], item_ref=ref, life_areas=_areas(ADD_AREAS)),
        _tool(REMOVE_LIFE_AREAS, "Remove the named life areas from the note.", ["item_ref", "life_areas"],
              item_ref=ref, life_areas=_areas(REMOVE_AREAS)),
        _tool(DELETE_NOTE, "Remove the note; its history is kept and undo brings it back.", ["item_ref"], item_ref=ref),
        _tool(UNDO_NOTE, "Put the note back as it was before its last change.", ["item_ref"], item_ref=ref),
        _tool(ERASE_NOTE, "Ask to erase the note's whole history: the owner gets a code to paste; nothing is erased "
              "now.", ["item_ref"], item_ref=ref),
    ]


__all__ = [
    "ADD_LIFE_AREAS", "ADD_TO_NOTE", "CLEAR_NOTE_TYPE", "CREATE_NOTE", "DELETE_NOTE", "ERASE_NOTE", "GET_NOTE",
    "NOTE_TOOLS", "NOTE_WRITE_TOOLS", "REMOVE_LIFE_AREAS", "REPLACE_NOTE_TEXT", "SET_NOTE_TYPE", "STATUS_SENTENCES",
    "UNDO_NOTE", "drive_note_tool_declarations", "erase_request_line", "note_receipt_line", "note_view_text",
]
