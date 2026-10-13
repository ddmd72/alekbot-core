"""
Drive index lean projections — what Search v2's streams read instead of whole records, chunks and hint documents
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.2.6, §25.13.3, frozen in §25.13.2). Pure data.

Full records are read only for the shown cards and their ancestors; windows read chunk text and hint items by id.

Public interface:
    ChunkId                     (item_id, generation, index) — one chunk or hint document of a user
    DriveRecordLean, DriveChunkLean, DriveHintLean
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .drive_index import RecordState, Scope

ChunkId = Tuple[str, int, int]


@dataclass(frozen=True)
class DriveRecordLean:
    """The record fields coverage, visibility and ordering need (§25.13.3). `valid_until` is the record's top-level
    validity date (§25.2.1), stored as a UTC timestamp so it can be indexed."""
    item_id: str
    parent_id: str
    kind: str
    effective_date: Optional[datetime]
    about_keys: Tuple[str, ...]
    name_tokens: Tuple[str, ...]
    state: RecordState
    scope: Scope
    has_card: bool
    indexed_generation: Optional[int]
    content_hash: Optional[str]
    valid_until: Optional[datetime]


@dataclass(frozen=True)
class DriveChunkLean:
    """A chunk for streams: no text, no keys, no vector (§25.13.3). Not fenced: the caller checks `generation`."""
    user_id: str
    item_id: str
    generation: int
    index: int
    kind: str


@dataclass(frozen=True)
class DriveHintLean:
    """A hint document for streams (§25.13.3): its keys, not its items or vector. Not fenced."""
    user_id: str
    item_id: str
    generation: int
    index: int
    kind: str
    hint_keys: Tuple[str, ...]
