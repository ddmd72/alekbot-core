"""One item's parent chain on one connection (MULTI_DRIVE_RFC §11.3): what the access policy reads to decide whether
an item lies inside a scope folder or the write folder. Built by `UserDriveService.ancestry` from the port's parent ids."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Ancestry:
    """`ids`: the item first, its nearest parent next, up to the root (non-empty). `reached_root`: the chain ended at
    the drive's root (the area root or the account root); False when it ended at an item with no reachable parent (a
    shared item) or was cut at `drive_search.MAX_CHAIN_DEPTH`."""
    ids: Tuple[str, ...]
    reached_root: bool

    def __post_init__(self) -> None:
        if not self.ids or not all(self.ids):
            raise ValueError("an ancestry holds the item id first and no empty id")

    @property
    def item_id(self) -> str:
        return self.ids[0]

    def contains(self, item_id: str) -> bool:
        """`item_id` is the item itself or one of its ancestors."""
        return item_id in self.ids
