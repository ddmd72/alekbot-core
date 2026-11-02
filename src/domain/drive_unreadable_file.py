"""
UnreadableFile — one file the index could not read, as the Cabinet's drive card lists it (USER_DRIVE_INDEXING_RFC §6).
The bot is never told about these files (decisions/drive_unreadable_files_cabinet_only.md); only the owner sees them.
Pure value object plus the one rule that names the reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from .drive_index import DriveIndexRecord, RecordState

FAILED_REASON = "failed"


@dataclass(frozen=True)
class UnreadableFile:
    name: str
    reason: str     # a Reason value of a metadata_only record, or "failed"
    web_url: str


def unreadable_reason(rec: DriveIndexRecord) -> str:
    """Why `rec` could not be read: the record's reason when it is `metadata_only`, `failed` when its state is FAILED."""
    if rec.state is RecordState.FAILED:
        return FAILED_REASON
    return rec.reason.value if rec.reason is not None else FAILED_REASON
