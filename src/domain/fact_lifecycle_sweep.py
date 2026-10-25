"""The janitor's rules (FACT_LIFECYCLE_RFC D4, D10): the breaker, the fields of a stale write, the log-only switch and
the daily ops line. Pure: the service reads and writes, this decides and formats."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Final, Optional, Sequence

from .due_fact import DueFact
from .entities import FactState
from .fact_lifecycle import lifecycle_event, lifecycle_fields, with_event
from .fact_lifecycle_day import FactLifecycleDay
from .fact_review_rules import POOL_CAP

BREAKER_REVIEW_AGE: Final[timedelta] = timedelta(hours=72)
BREAKER_BACKLOG: Final[int] = POOL_CAP
_OFF_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


def breaker_reason(stale_count: int, last_review_at: Optional[datetime], now: datetime) -> Optional[str]:
    """Why the janitor must stale nothing for an account, or None when it may.

    Trips when the account has stale facts and the last successful review is older than 72 h (or there is none), or when
    the stale backlog is over 50. The edges are exclusive: exactly 72 h and exactly 50 facts do not trip.
    """
    if stale_count > 0 and (last_review_at is None or now - last_review_at > BREAKER_REVIEW_AGE):
        return f"{stale_count} stale facts and no successful review in the last 72 h"
    if stale_count > BREAKER_BACKLOG:
        return f"stale backlog {stale_count} is over {BREAKER_BACKLOG}"
    return None


def stale_headroom(stale_count: int) -> int:
    """How many more facts one run may stale for an account that already has `stale_count` stale facts: the backlog cap
    (BREAKER_BACKLOG) minus the backlog before the run, never below 0."""
    return max(0, BREAKER_BACKLOG - stale_count)


def stale_fields(due: DueFact, now: datetime) -> Dict[str, Any]:
    """The fields that move a due fact to stale, and nothing else: state, `stale_since`, `last_updated` and the trail."""
    event = lifecycle_event(now, FactState.CURRENT, FactState.STALE, "janitor", f"expired {due.expires_at.isoformat()}")
    return {
        **lifecycle_fields(FactState.STALE, now),
        "stale_since": now,
        "last_updated": now,
        "lifecycle_events": with_event(due.lifecycle_events, event),
    }


def sweep_dry_run(raw: Optional[str]) -> bool:
    """Log-only unless `raw` is an explicit off value (0, false, no, off): unset, empty or anything else stays log-only."""
    return (raw or "").strip().lower() not in _OFF_VALUES


def daily_line(run_at: datetime, rows: Sequence[FactLifecycleDay], dry_run: bool) -> Optional[str]:
    """The day's ops line for the run at `run_at`: one line per account with something to say, or None when no row has.

    `expiry_tool_errors` of a row is the figure the caller chose to show (the service passes yesterday's).
    """
    lines = [_account_line(row) for row in sorted(rows, key=lambda r: r.account_id) if _worth_a_line(row)]
    if not lines:
        return None
    moment = run_at.astimezone(timezone.utc) if run_at.tzinfo is not None else run_at
    header = f"🧹 Fact lifecycle — run {moment:%Y-%m-%d %H:%M} UTC" + (" · log-only" if dry_run else "")
    return "\n".join([header, *lines])


def _worth_a_line(row: FactLifecycleDay) -> bool:
    numbers = (row.due, row.newly_due, row.no_decision, row.staled, row.would_stale, row.deferred,
               row.stale_conflicts, row.backlog, row.expiry_tool_errors)
    return any(numbers) or row.breaker not in ("", "ok")


def _account_line(row: FactLifecycleDay) -> str:
    oldest = f" (oldest {row.oldest_stale_days}d)" if row.oldest_stale_days is not None else ""
    return (
        f"{row.account_id[:8]}: due {row.due} (new {row.newly_due}, no decision {row.no_decision})"
        f" | staled {row.staled} / would stale {row.would_stale} / deferred {row.deferred}"
        f" | conflicts {row.stale_conflicts}"
        f" | breaker {row.breaker or 'ok'}"
        f" | backlog {row.backlog}{oldest}"
        f" | expiry tool errors yesterday {row.expiry_tool_errors}"
    )
