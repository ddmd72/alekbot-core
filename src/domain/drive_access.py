"""
Drive access — the pure decisions about files the bot cannot open that are not the planner's
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §26.2, D33, Δ49, Δ53, Δ56). No I/O, no clock.

Public interface:
    AccessCounts                                  the tick's counters over the marked records
    access_counts(records, admitted=None, triaged=None) -> AccessCounts
    notice_due(rec, observed_as_of) -> bool       a file the fan-out may tell the user about now
    notice_batch(records, observed_as_of, limit) -> List[DriveIndexRecord]
    notice_paths(records, lookup, root_item_id) -> List[str]
    notice_file(paths) -> bytes;  NOTICE_FILENAME
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, FrozenSet, Iterable, List, Optional, Sequence

from .drive_index import DriveIndexRecord, inaccessible
from .drive_search import is_not_read_yet, resolve_chain

NOTICE_FILENAME = "drive-files-the-bot-cannot-open.txt"


@dataclass(frozen=True)
class AccessCounts:
    files: int = 0          # files (never folders) the bot cannot open: the coverage line's count (Δ56)
    unnotified: int = 0     # of them, those whose current marking the user was not told of: the fan-out's gate
    waiting: int = 0        # of them, those `count_unread` counts as not read yet: taken out of that count (Δ56)


def access_counts(records: Iterable[DriveIndexRecord], admitted: Optional[FrozenSet[str]] = None,
                  triaged: Optional[FrozenSet[str]] = None,
                  transcribed: Optional[FrozenSet[str]] = None) -> AccessCounts:
    files = unnotified = waiting = 0
    for rec in records:
        if rec.is_folder or not inaccessible(rec):
            continue
        files += 1
        unnotified += int(rec.inaccessible_notified_since != rec.inaccessible_since)
        waiting += int(is_not_read_yet(rec, admitted, triaged, transcribed))
    return AccessCounts(files=files, unnotified=unnotified, waiting=waiting)


def notice_due(rec: DriveIndexRecord, observed_as_of: Optional[datetime]) -> bool:
    """A file marked inaccessible whose marking the user was not told of, and a delta round that started after the
    marking has completed (Δ49): a file deleted between delta and the download is gone (its tombstone came in that
    round) before anyone is told it is closed. Folders are never in the notice: their files are."""
    since = rec.inaccessible_since
    return (since is not None and not rec.is_folder and rec.inaccessible_notified_since != since
            and observed_as_of is not None and observed_as_of > since)


def notice_batch(records: Sequence[DriveIndexRecord], observed_as_of: Optional[datetime],
                 limit: int) -> List[DriveIndexRecord]:
    return [rec for rec in records if notice_due(rec, observed_as_of)][:limit]


def notice_paths(records: Sequence[DriveIndexRecord], lookup: Callable[[str], Optional[DriveIndexRecord]],
                 root_item_id: str) -> List[str]:
    """Each file's path from its parent chain (§3.1: paths are derived at read time), its name when the chain is hidden
    or broken; sorted, so the list reads by folder."""
    paths = []
    for rec in records:
        chain = resolve_chain(rec, lookup, root_item_id, require_card=False)
        paths.append(chain[0] if chain is not None else rec.name)
    return sorted(paths, key=str.casefold)


def notice_file(paths: Sequence[str]) -> bytes:
    """The attached list (Δ53): one path per line, UTF-8. File names travel only here, never in the chat text."""
    return ("\n".join(paths) + "\n").encode("utf-8")
