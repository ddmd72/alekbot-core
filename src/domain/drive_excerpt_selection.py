"""What one passages call selected (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A6). Pure."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Tuple

from .drive_index_lean import ChunkId


@dataclass(frozen=True)
class Selection:
    """`chunks`: the new chunks, in rank order, each a keeper's (identical copies mapped onto it); `repeated`: chunks
    shown earlier in the run that ranked again; `also_at`: keeper item id -> the item ids of its identical copies,
    in rank order."""
    chunks: Tuple[ChunkId, ...] = ()
    repeated: Tuple[ChunkId, ...] = ()
    also_at: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
