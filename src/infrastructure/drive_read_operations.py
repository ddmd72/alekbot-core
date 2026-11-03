"""The drive reads an outside client may run (the drive connector, DRIVE_MCP concept, engineering call 6).

`DriveReadOperations` wraps one user's `DriveOperations` and exposes exactly five of its `READ_METHODS`: `search`,
`find_passages`, `read_passages`, `list_folder` and `open`. Nothing else is reachable through it: no mutation, no
`preflight`, `folder_path`, `fetch_stored` or `search_deleted`, and no attribute pass-through (a test pins the set).

`open` always runs with `withhold_media=True`: no outside client can make an open download a video or an untranscribed
recording, or post a video to the owner's chat (the outcome carries `DriveOutcome.withheld` instead); it takes no
`original` either (a recording's link is not the connector's). Every other argument goes through unchanged, and each
method returns the `DriveOutcome` the wrapped call returns.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, FrozenSet, Mapping, Optional, Tuple

from ..domain.drive_index_lean import ChunkId
from ..domain.drive_outcome import DriveOutcome

if TYPE_CHECKING:
    from .drive_operations import DriveOperations


class DriveReadOperations:
    """Five reads of one user's `DriveOperations`, nothing more."""

    __slots__ = ("_ops",)

    def __init__(self, operations: "DriveOperations") -> None:
        self._ops = operations

    async def search(self, context: Mapping[str, Any], user_id: str, *, query: Any = None, folder: Any = None,
                     kind: Any = None, date_from: Any = None, date_to: Any = None,
                     drives: Any = None) -> DriveOutcome:
        return await self._ops.search(context, user_id, query=query, folder=folder, kind=kind, date_from=date_from,
                                      date_to=date_to, drives=drives)

    async def find_passages(self, context: Mapping[str, Any], user_id: str, *, question: Any, folder: Any = None,
                            drives: Any = None, file_refs: Any = None,
                            shown: FrozenSet[Tuple[str, ChunkId]] = frozenset(),
                            shown_folders: FrozenSet[str] = frozenset(),
                            described_files: FrozenSet[str] = frozenset()) -> DriveOutcome:
        return await self._ops.find_passages(context, user_id, question=question, folder=folder, drives=drives,
                                             file_refs=file_refs, shown=shown, shown_folders=shown_folders,
                                             described_files=described_files)

    async def read_passages(self, context: Mapping[str, Any], user_id: str, *, file_ref: Any, passages: Any,
                            shown_folders: FrozenSet[str] = frozenset(),
                            described_files: FrozenSet[str] = frozenset()) -> DriveOutcome:
        return await self._ops.read_passages(context, user_id, file_ref=file_ref, passages=passages,
                                             shown_folders=shown_folders, described_files=described_files)

    async def list_folder(self, context: Mapping[str, Any], user_id: str, folder: Any = None) -> DriveOutcome:
        return await self._ops.list_folder(context, user_id, folder)

    async def open(self, context: Mapping[str, Any], user_id: str, ref: str, *,
                   deadline_at: Optional[float] = None) -> DriveOutcome:
        """The media pre-check is always on (`withhold_media=True`)."""
        return await self._ops.open(context, user_id, ref, deadline_at=deadline_at, withhold_media=True)
