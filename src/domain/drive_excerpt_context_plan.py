"""What one render prints of the "where it comes from" context (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A10,
A18): returned with the text, so the run's ledger records what the reader has seen without parsing text. Pure."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, FrozenSet, Mapping, Tuple

from .drive_excerpt_folder_note import FolderNote


@dataclass(frozen=True)
class ContextPlan:
    """`before`: file item id -> the folder lines printed right before it; `described`: file item ids whose
    description and own note print now; `dropped`: folder paths the cap left out; `shown_after`: folder item ids
    shown once this render is read (the ones shown before included); `has_owner_line`: a folder line or an own note
    printed."""
    EMPTY: ClassVar["ContextPlan"]

    before: Mapping[str, Tuple[FolderNote, ...]] = field(default_factory=dict)
    described: FrozenSet[str] = frozenset()
    dropped: Tuple[str, ...] = ()
    shown_after: FrozenSet[str] = frozenset()
    has_owner_line: bool = False


ContextPlan.EMPTY = ContextPlan()
