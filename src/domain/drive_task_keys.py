"""
Cloud Tasks dedup keys and payloads for `drive_sync` (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §7.1;
RFC Δ2-Δ4). A key, once used, is reserved by Cloud Tasks for up to a day, so every family carries a
component that is never reused.

Per connection (MULTI_DRIVE_RFC §8): `tick_task_key`, `continuation_task_key`, `nudge_task_key`, `toggle_task_key`
take a `ConnectionScope` and start with the connection id, so two connections of one user never share a key; the
payload is `{user_id, connection_id}` (`scope_payload`), and `payload_scope` refuses (None) a payload without either.
The `user_id` forms above them are the single-drive ones, kept until their callers move (slice 1b).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

from .connection_scope import ConnectionScope
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


def tick_task_key(scope: ConnectionScope, now: datetime) -> str:
    bucket = int(now.timestamp() // TICK_INTERVAL.total_seconds())
    return f"{scope.connection_id}:tick:{bucket}"


def continuation_task_key(scope: ConnectionScope, tick_id: str) -> str:
    return f"{scope.connection_id}:cont:{tick_id}"


def nudge_task_key(scope: ConnectionScope, mutation_id: str, attempt: int) -> str:
    return f"{scope.connection_id}:nudge:{mutation_id}" + (f":r{attempt}" if attempt else "")


def toggle_task_key(scope: ConnectionScope, now: datetime) -> str:
    return f"{scope.connection_id}:toggle:{int(now.timestamp() * 1000)}"


def scope_payload(scope: ConnectionScope, nudge: Optional[Nudge] = None) -> Dict[str, Any]:
    """The `drive_sync` payload of one connection: `{user_id, connection_id}`, plus the nudge when there is one."""
    payload: Dict[str, Any] = {"user_id": scope.user_id, "connection_id": scope.connection_id}
    if nudge is not None:
        payload["nudge"] = {"mutation_id": nudge.mutation_id, "item_ids": list(nudge.item_ids),
                            "attempt": nudge.attempt}
    return payload


def payload_scope(payload: Mapping[str, Any]) -> Optional[ConnectionScope]:
    """The connection a `drive_sync` payload is for; None (the task is refused) when `user_id` or `connection_id` is
    missing, empty, not a string, or not a valid connection id."""
    user_id, connection_id = payload.get("user_id"), payload.get("connection_id")
    if not isinstance(user_id, str) or not isinstance(connection_id, str):
        return None
    try:
        return ConnectionScope(user_id=user_id, connection_id=connection_id)
    except ValueError:
        return None


def parse_nudge(payload: Dict[str, Any]) -> Optional[Nudge]:
    raw = payload.get("nudge")
    if not isinstance(raw, dict) or not raw.get("mutation_id"):
        return None
    return Nudge(mutation_id=str(raw["mutation_id"]), item_ids=tuple(str(i) for i in raw.get("item_ids") or ()),
                 attempt=int(raw.get("attempt") or 0))
