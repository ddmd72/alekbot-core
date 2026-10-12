"""
Cloud Tasks dedup keys and payloads for `drive_sync` (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7.1;
RFC Δ2-Δ4). A key, once used, is reserved by Cloud Tasks for up to a day, so every family carries a
component that is never reused.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .drive_index import Nudge
from .drive_index_params import TICK_INTERVAL

DRIVE_SYNC_TASK = "drive_sync"


def tick_key(user_id: str, now: datetime) -> str:
    bucket = int(now.timestamp() // TICK_INTERVAL.total_seconds())
    return f"{user_id}:tick:{bucket}"


def continuation_key(user_id: str, tick_id: str) -> str:
    return f"{user_id}:cont:{tick_id}"


def nudge_key(user_id: str, mutation_id: str, attempt: int) -> str:
    return f"{user_id}:nudge:{mutation_id}" + (f":r{attempt}" if attempt else "")


def toggle_key(user_id: str, now: datetime) -> str:
    return f"{user_id}:toggle:{int(now.timestamp() * 1000)}"


def tick_payload(user_id: str, nudge: Optional[Nudge] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"user_id": user_id}
    if nudge is not None:
        payload["nudge"] = {"mutation_id": nudge.mutation_id, "item_ids": list(nudge.item_ids),
                            "attempt": nudge.attempt}
    return payload


def parse_nudge(payload: Dict[str, Any]) -> Optional[Nudge]:
    raw = payload.get("nudge")
    if not isinstance(raw, dict) or not raw.get("mutation_id"):
        return None
    return Nudge(mutation_id=str(raw["mutation_id"]), item_ids=tuple(str(i) for i in raw.get("item_ids") or ()),
                 attempt=int(raw.get("attempt") or 0))
