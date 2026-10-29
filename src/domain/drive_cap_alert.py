"""The fan-out's cap alert (USER_DRIVE_INDEXING_RFC §5.4, §12.1): files of a user held by a per-user cap.

Two caps hold files: the card cap (MAX_CARD_RECORDS_PER_USER; the record carries `card_capped`, its content and triage
wait) and the chunk cap (MAX_CHUNKS_PER_USER; the record is `metadata_only / user_chunk_cap`). Each cap has its own
once-only flag on the sync state, like the staleness alert's `stale_alerted`: the alert is posted when a cap goes from no
held file to some, and again only after the cap held none for a run in between. This module decides; the fan-out counts,
posts and writes the flags.

Public interface:
    CapCounts(card, chunk)                                    files held by each cap
    cap_flag_changes(counts, *, card_alerted, chunk_alerted)  the flags that must move, cap -> new value
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

CARD_CAP = "card"
CHUNK_CAP = "chunk"


@dataclass(frozen=True)
class CapCounts:
    """Files held by each per-user cap: `card` records with `card_capped`, `chunk` records `metadata_only` with reason
    `user_chunk_cap`. A record can be in both."""
    card: int = 0
    chunk: int = 0


def cap_flag_changes(counts: CapCounts, *, card_alerted: bool, chunk_alerted: bool) -> Dict[str, bool]:
    """Per cap whose flag must change, its new value, card cap first: True when the cap holds files and was not alerted
    (post the alert, then raise the flag), False when it holds none and was alerted (lower the flag, nothing posted).
    A cap whose flag already matches is left out, so a repeat run changes nothing."""
    changes: Dict[str, bool] = {}
    for cap, held, alerted in ((CARD_CAP, counts.card, card_alerted), (CHUNK_CAP, counts.chunk, chunk_alerted)):
        if (held > 0) != alerted:
            changes[cap] = held > 0
    return changes
