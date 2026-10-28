"""
Drive note change — one partial edit of a note, validated at construction and applied to the current triple
(specialist RFC §9.2, design §4.1; Δ5 decided: a change that would leave a note empty is refused).
Pure: domain modules only; no I/O.

Public interface:
    NoteChange                      frozen; one classmethod constructor per NoteChangeKind
    apply_change(current, change) -> NoteContent      the triple after the change, or DriveNoteRefused
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

from src.domain.drive_note import (
    LIFE_AREAS_MAX, DriveNoteRefused, NoteContent, NoteRefusal, check_note_text, normalize_note_text,
    note_word_count, resolve_doc_type, resolve_life_areas,
)
from src.domain.drive_note_change_kind import ADDITIVE_KINDS, NoteChangeKind

_EMPTY_NOTE_MESSAGE = "This would leave the note empty: use delete_note."


def _empty_change(what: str) -> DriveNoteRefused:
    return DriveNoteRefused(NoteRefusal.EMPTY_CHANGE, f"{what} is empty: nothing to change (to remove the note, "
                                                      f"use delete_note).")


@dataclass(frozen=True)
class NoteChange:
    kind: NoteChangeKind
    text: str = ""
    doc_type: str = ""
    life_areas: Tuple[str, ...] = ()

    @classmethod
    def append_text(cls, text: str) -> "NoteChange":
        checked = check_note_text(text)
        if not checked:
            raise _empty_change("The text to append")
        return cls(NoteChangeKind.APPEND_TEXT, text=checked)

    @classmethod
    def replace_text(cls, text: str) -> "NoteChange":
        checked = check_note_text(text)
        if not checked:
            raise _empty_change("The new text")
        return cls(NoteChangeKind.REPLACE_TEXT, text=checked)

    @classmethod
    def set_type(cls, doc_type: str) -> "NoteChange":
        resolved = resolve_doc_type(doc_type)
        if not resolved:
            raise _empty_change("The document type")
        return cls(NoteChangeKind.SET_TYPE, doc_type=resolved)

    @classmethod
    def clear_type(cls) -> "NoteChange":
        return cls(NoteChangeKind.CLEAR_TYPE)

    @classmethod
    def add_areas(cls, areas: Sequence[str]) -> "NoteChange":
        resolved = resolve_life_areas(areas)
        if not resolved:
            raise _empty_change("The list of life areas")
        return cls(NoteChangeKind.ADD_AREAS, life_areas=resolved)

    @classmethod
    def remove_areas(cls, areas: Sequence[str]) -> "NoteChange":
        resolved = resolve_life_areas(areas)
        if not resolved:
            raise _empty_change("The list of life areas")
        return cls(NoteChangeKind.REMOVE_AREAS, life_areas=resolved)

    @property
    def additive(self) -> bool:
        return self.kind in ADDITIVE_KINDS


def apply_change(current: NoteContent, change: NoteChange) -> NoteContent:
    """The triple after `change`. An absent value to remove or clear is a no-op. Raises DriveNoteRefused: TOO_LONG
    (an append that overflows; carries the current length too), TOO_MANY_LIFE_AREAS, EMPTY_CHANGE (the result
    would be empty)."""
    kind = change.kind
    result = current
    if kind == NoteChangeKind.APPEND_TEXT:
        try:
            joined = check_note_text(f"{current.text} {change.text}")
        except DriveNoteRefused as refused:
            if refused.reason != NoteRefusal.TOO_LONG:
                raise
            current_words, current_chars = note_word_count(current.text), len(normalize_note_text(current.text))
            detail = dict(refused.detail, current_words=current_words, current_chars=current_chars)
            raise DriveNoteRefused(
                NoteRefusal.TOO_LONG,
                f"{refused} The note already holds {current_words} words, {current_chars} characters: append less "
                f"or replace the text.", **detail) from refused
        result = NoteContent(joined, current.doc_type, current.life_areas)
    elif kind == NoteChangeKind.REPLACE_TEXT:
        result = NoteContent(change.text, current.doc_type, current.life_areas)
    elif kind == NoteChangeKind.SET_TYPE:
        result = NoteContent(current.text, change.doc_type, current.life_areas)
    elif kind == NoteChangeKind.CLEAR_TYPE:
        result = NoteContent(current.text, "", current.life_areas)
    elif kind == NoteChangeKind.ADD_AREAS:
        merged = current.life_areas + tuple(a for a in change.life_areas if a not in current.life_areas)
        if len(merged) > LIFE_AREAS_MAX:
            raise DriveNoteRefused(
                NoteRefusal.TOO_MANY_LIFE_AREAS,
                f"At most {LIFE_AREAS_MAX} life areas; the note has {', '.join(current.life_areas)} and this adds "
                f"{', '.join(a for a in change.life_areas if a not in current.life_areas)}: remove one first.",
                count=len(merged), max=LIFE_AREAS_MAX)
        result = NoteContent(current.text, current.doc_type, merged)
    elif kind == NoteChangeKind.REMOVE_AREAS:
        result = NoteContent(current.text, current.doc_type,
                             tuple(a for a in current.life_areas if a not in change.life_areas))
    if result.is_empty:
        raise DriveNoteRefused(NoteRefusal.EMPTY_CHANGE, _EMPTY_NOTE_MESSAGE)
    return result


__all__ = ["NoteChange", "apply_change"]
