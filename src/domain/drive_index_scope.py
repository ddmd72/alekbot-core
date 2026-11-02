"""The scope an index covers (MULTI_DRIVE_RFC §11.3, E20). Pure.

A connection's feed reads either an area with a root (an App Folder: `FeedScope.AREA`) or the whole account
(`FeedScope.ACCOUNT`). What the index keeps of it is the connection's scope: every folder the owner selected, the whole
drive, or the default (the area root for AREA; nothing for ACCOUNT). When the scope covers everything the planner needs
no ancestry; otherwise a change is kept only when its parent chain reaches a scope folder.

Public interface:
    IndexScope(feed, whole_drive, folder_ids): .covers_everything, IndexScope.of(connection, capabilities)
    ANCESTOR                                                  (verdict sentinel)
    verdict(item_id, parent_id, scope, root_item_id, known, parents) -> Union[bool, str]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Mapping, Union

from .drive_capabilities import DriveCapabilities
from .drive_connection import DriveConnection
from .drive_feed_scope import FeedScope
from .drive_search import MAX_CHAIN_DEPTH

# `verdict` answer for a folder on the path from the drive's root to a scope folder: stored as a skeleton record
ANCESTOR = "\x00scope_ancestor"


@dataclass(frozen=True)
class IndexScope:
    feed: FeedScope
    whole_drive: bool
    folder_ids: FrozenSet[str]

    @property
    def covers_everything(self) -> bool:
        """Whole drive, or an area whose root is the default scope: no change is filtered, no ancestry is read."""
        return self.whole_drive or (self.feed is FeedScope.AREA and not self.folder_ids)

    @classmethod
    def of(cls, connection: DriveConnection, capabilities: DriveCapabilities) -> "IndexScope":
        return cls(feed=capabilities.feed_scope, whole_drive=connection.scope_whole_drive,
                   folder_ids=frozenset(connection.scope_folder_ids))


def verdict(item_id: str, parent_id: str, scope: IndexScope, root_item_id: str,
            known: Mapping[str, "bool | None"], parents: Mapping[str, str]) -> Union[bool, str]:
    """Is the item inside the scope? True: it is a scope folder, or its chain reaches one. False: the chain reaches the
    drive's root (or ends at "" without whole drive). `ANCESTOR`: the item is a folder on the path from the root to a
    scope folder. Otherwise the id of the first ancestor the caller must read (its parent id goes into `parents`).

    `known`: folder id -> verdict already taken (True / False), or None for a scope ancestor; `parents`: item id -> its
    parent id, as far as the caller has them. A chain cut at MAX_CHAIN_DEPTH is out."""
    if scope.whole_drive:
        return True
    if item_id in scope.folder_ids:
        return True
    if item_id in known and known[item_id] is None:
        return ANCESTOR
    current = parent_id
    for _ in range(MAX_CHAIN_DEPTH):
        if current in scope.folder_ids:
            return True
        if current == "":
            return False   # no reachable parent: only the whole drive keeps such an item
        if current == root_item_id:
            return False
        if current in known and known[current] is not None:
            return bool(known[current])
        if current not in parents:
            return current
        current = parents[current]
    return False
