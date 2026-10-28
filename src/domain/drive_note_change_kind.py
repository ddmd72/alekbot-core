"""
Drive note change kinds — what one note-writing tool does to the stored triple (specialist RFC §9.2, design §4.1).
Pure: stdlib only.
"""
from __future__ import annotations

from enum import Enum
from typing import FrozenSet


class NoteChangeKind(str, Enum):
    APPEND_TEXT = "append_text"
    REPLACE_TEXT = "replace_text"
    SET_TYPE = "set_type"
    CLEAR_TYPE = "clear_type"
    ADD_AREAS = "add_areas"
    REMOVE_AREAS = "remove_areas"


# The kinds that only add to a note, so they may create one where none exists.
ADDITIVE_KINDS: FrozenSet[NoteChangeKind] = frozenset(
    {NoteChangeKind.APPEND_TEXT, NoteChangeKind.SET_TYPE, NoteChangeKind.ADD_AREAS})

__all__ = ["ADDITIVE_KINDS", "NoteChangeKind"]
