"""The drive reads an outside client may run (the drive connector, DRIVE_MCP concept, engineering call 6).

`DriveReadOperations` wraps one user's `DriveOperations` and exposes exactly five of its `READ_METHODS`: `search`,
`find_passages`, `read_passages`, `list_folder` and `open`. Nothing else is reachable through it: no mutation, no
`preflight`, `folder_path`, `fetch_stored` or `search_deleted`, and no attribute pass-through (a test pins the set).

`open` refuses every ref that is not a `drive:` ref before any call (a chat upload, a delivered document, `workspace:`,
`skill:` lie outside the connector's folder access): class REFUSED, no text, no withheld code; the connector answers
its plain neutral refusal, the same as for an item outside its folders or one that does not exist. A `drive:` ref always runs with `withhold_media=True`: no outside client can make an open download a video or an untranscribed
recording, or post a video to the owner's chat (the outcome carries `DriveOutcome.withheld` instead); it takes no
`original` either (a recording's link is not the connector's). Every other argument goes through unchanged (the
connector's `wide` on `search` and `find_passages`, and `batch` on `find_passages`, DRIVE_MCP_CONNECTOR_RFC §14.2,
included), and each method returns the `DriveOutcome` the wrapped call returns.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Mapping, Optional, Tuple

from ..domain import drive_ref
from ..domain.drive_index_lean import ChunkId
from ..domain.drive_op import DriveOp
from ..domain.drive_outcome import DriveOutcome
from ..domain.drive_outcome_class import OutcomeClass
from ..utils.logger import logger

if TYPE_CHECKING:
    from .drive_operations import DriveOperations


def _given(**kwargs: Any) -> Dict[str, Any]:
    """The connector's optional arguments that were given: one not given is not passed, so the wrapped call is the
    call it was before they existed."""
    return {key: value for key, value in kwargs.items() if value is not None}


class DriveReadOperations:
    """Five reads of one user's `DriveOperations`, nothing more."""

    __slots__ = ("_ops",)

    def __init__(self, operations: "DriveOperations") -> None:
        self._ops = operations

    async def search(self, context: Mapping[str, Any], user_id: str, *, query: Any = None, folder: Any = None,
                     kind: Any = None, date_from: Any = None, date_to: Any = None,
                     drives: Any = None, wide: Any = None) -> DriveOutcome:
        return await self._ops.search(context, user_id, query=query, folder=folder, kind=kind, date_from=date_from,
                                      date_to=date_to, drives=drives, **_given(wide=wide))

    async def find_passages(self, context: Mapping[str, Any], user_id: str, *, question: Any, folder: Any = None,
                            drives: Any = None, file_refs: Any = None,
                            shown: FrozenSet[Tuple[str, ChunkId]] = frozenset(),
                            shown_folders: FrozenSet[str] = frozenset(),
                            described_files: FrozenSet[str] = frozenset(), wide: Any = None,
                            batch: Any = None) -> DriveOutcome:
        return await self._ops.find_passages(context, user_id, question=question, folder=folder, drives=drives,
                                             file_refs=file_refs, shown=shown, shown_folders=shown_folders,
                                             described_files=described_files, **_given(wide=wide, batch=batch))

    async def read_passages(self, context: Mapping[str, Any], user_id: str, *, file_ref: Any, passages: Any,
                            shown_folders: FrozenSet[str] = frozenset(),
                            described_files: FrozenSet[str] = frozenset()) -> DriveOutcome:
        return await self._ops.read_passages(context, user_id, file_ref=file_ref, passages=passages,
                                             shown_folders=shown_folders, described_files=described_files)

    async def list_folder(self, context: Mapping[str, Any], user_id: str, folder: Any = None) -> DriveOutcome:
        return await self._ops.list_folder(context, user_id, folder)

    async def open(self, context: Mapping[str, Any], user_id: str, ref: str, *,
                   deadline_at: Optional[float] = None) -> DriveOutcome:
        """A `drive:` ref only, refused otherwise before any call; the media pre-check is always on."""
        if not drive_ref.is_drive_ref(ref or ""):
            logger.info("[DriveReadOperations] open refused: not a drive ref")
            return DriveOutcome(op=DriveOp.OPEN, cls=OutcomeClass.REFUSED, text="", success=False)
        return await self._ops.open(context, user_id, ref, deadline_at=deadline_at, withhold_media=True)
