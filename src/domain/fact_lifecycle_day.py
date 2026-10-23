from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

# Written by the janitor as one snapshot of its run (the last run of the day wins).
SNAPSHOT_FIELDS = frozenset({
    "run_at", "due", "newly_due", "no_decision", "staled", "would_stale", "deferred", "stale_conflicts",
    "breaker", "backlog", "oldest_stale_days",
})
# Added to by whoever meets the event (`Increment`), never overwritten by a snapshot.
COUNTER_FIELDS = frozenset({"expiry_tool_errors"})


_SNAPSHOT_COUNTS = frozenset({
    "due", "newly_due", "no_decision", "staled", "would_stale", "deferred", "stale_conflicts", "backlog",
})


def check_snapshot(values: Mapping[str, Any]) -> None:
    """Raise ValueError unless `values` is a whole janitor snapshot: exactly SNAPSHOT_FIELDS, each of its type.

    A snapshot is written with merge, so a missing field would keep the earlier run's value and break "the last run wins".
    `oldest_stale_days` may be None (it is stored as null, replacing an earlier age).
    """
    missing = sorted(SNAPSHOT_FIELDS - set(values))
    unknown = sorted(set(values) - SNAPSHOT_FIELDS)
    if missing or unknown:
        raise ValueError(f"a snapshot has exactly the snapshot fields; missing {missing}, not snapshot fields {unknown}")
    if not isinstance(values["run_at"], datetime):
        raise ValueError("run_at must be a datetime")
    if not isinstance(values["breaker"], str):
        raise ValueError("breaker must be a string")
    for name in sorted(_SNAPSHOT_COUNTS):
        if not _is_count(values[name]):
            raise ValueError(f"{name} must be a non-negative integer")
    if values["oldest_stale_days"] is not None and not _is_count(values["oldest_stale_days"]):
        raise ValueError("oldest_stale_days must be a non-negative integer or None")


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def day_doc_id(account_id: str, day: date) -> str:
    """The id of one account's document for one UTC day."""
    return f"{account_id}:{day.isoformat()}"


@dataclass(frozen=True)
class FactLifecycleDay:
    """One account's lifecycle numbers for one UTC day: the janitor's last run and the tool-error counters."""

    account_id: str
    day: date
    run_at: Optional[datetime] = None
    due: int = 0
    newly_due: int = 0
    no_decision: int = 0
    staled: int = 0
    would_stale: int = 0
    deferred: int = 0
    stale_conflicts: int = 0
    breaker: str = ""
    backlog: int = 0
    oldest_stale_days: Optional[int] = None
    expiry_tool_errors: int = 0

    @classmethod
    def from_document(cls, data: Mapping[str, Any]) -> "FactLifecycleDay":
        """A day row from its stored document; a missing or unreadable number reads as 0."""
        return cls(
            account_id=str(data.get("account_id", "")),
            day=date.fromisoformat(str(data["day"])),
            run_at=data.get("run_at") if isinstance(data.get("run_at"), datetime) else None,
            due=_count(data.get("due")),
            newly_due=_count(data.get("newly_due")),
            no_decision=_count(data.get("no_decision")),
            staled=_count(data.get("staled")),
            would_stale=_count(data.get("would_stale")),
            deferred=_count(data.get("deferred")),
            stale_conflicts=_count(data.get("stale_conflicts")),
            breaker=str(data.get("breaker") or ""),
            backlog=_count(data.get("backlog")),
            oldest_stale_days=_count(data["oldest_stale_days"]) if data.get("oldest_stale_days") is not None else None,
            expiry_tool_errors=_count(data.get("expiry_tool_errors")),
        )


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)
