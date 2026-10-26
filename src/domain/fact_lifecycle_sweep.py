"""The janitor's rules (FACT_LIFECYCLE_RFC D4, D10): the plan of one account's run, the breaker, the fields of a stale
write, the log-only switch and the daily ops line. Pure: the service reads and writes, this decides and formats."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AbstractSet, Any, Dict, Final, Mapping, Optional, Sequence, Tuple

from .due_fact import DueFact
from .entities import FactState
from .env_switch import is_off_value
from .fact_lifecycle import lifecycle_event, lifecycle_fields, with_event
from .fact_lifecycle_day import FactLifecycleDay
from .fact_review_rules import POOL_CAP
from .fact_write_context import as_utc
from .settings import DEFAULT_FACT_REVIEW_INTERVAL_DAYS

BREAKER_REVIEW_AGE: Final[timedelta] = timedelta(hours=72)      # past the review interval; the breaker trips beyond interval + this
BREAKER_BACKLOG: Final[int] = POOL_CAP
PURGE_OVERDUE_AFTER: Final[timedelta] = timedelta(days=3)      # a closed question or digest claim still stored this long past its expiry is reported

# The breaker as the snapshot and the ops line name it: `ok`, `tripped` (write mode), `would trip` (log-only mode).
BREAKER_OK: Final[str] = "ok"
BREAKER_TRIPPED: Final[str] = "tripped"
BREAKER_WOULD_TRIP: Final[str] = "would trip"


@dataclass(frozen=True)
class SweepPlan:
    """What one run does for one account, decided before anything is written.

    `ordered`: the facts it may stale, oldest expiry first (ties by id) — those with an expiry decision, minus the facts
    of open questions. `no_decision`, `newly_due`: counted over the due facts that are not waiting. `due_waiting`: due
    facts whose question is open (the review holds them). `headroom`: how many more facts the run may stale.
    `breaker_reason`: why it must stale nothing, or None. `deferred`: the facts of `ordered` the run leaves to the next
    one — 0 while the breaker holds the run. A write run whose staling conflicts reports fewer deferred: the conflicts
    take their place (a conflict does not use headroom).
    """

    ordered: Tuple[DueFact, ...]
    no_decision: int
    newly_due: int
    due_waiting: int
    headroom: int
    breaker_reason: Optional[str]
    deferred: int


def plan_sweep(
    dues: Sequence[DueFact],
    stale_count: int,
    open_fact_ids: AbstractSet[str],
    previous_run: Optional[datetime],
    now: datetime,
    *,
    pool: Optional[int] = None,
    last_review_at: Optional[datetime] = None,
    interval: int = DEFAULT_FACT_REVIEW_INTERVAL_DAYS,
) -> SweepPlan:
    """The plan for one account: `dues` are its current facts past their expiry, `stale_count` its stale backlog,
    `open_fact_ids` the facts of its open questions, `previous_run` when its previous snapshot was taken.

    `pool` is the facts in the review's hands, the stale ones plus those of open questions (None: the stale backlog
    alone, no question open); it sets the cap (`stale_headroom`) and the breaker. `last_review_at` is the account's last
    successful review and `interval` its review interval in days, for the breaker.

    A due fact without an expiry decision (a document written before the expiry model) is never staled; a due fact in
    `open_fact_ids` waits for its question. The same `deferred` rule serves both modes.
    """
    held = stale_count if pool is None else pool
    waiting = [due for due in dues if due.fact_id in open_fact_ids]
    free = [due for due in dues if due.fact_id not in open_fact_ids]
    ordered = tuple(sorted(
        (due for due in free if due.expiry_kind is not None), key=lambda due: (as_utc(due.expires_at), due.fact_id)
    ))
    reason = breaker_reason(stale_count, held, last_review_at, now, interval)
    headroom = stale_headroom(held)
    return SweepPlan(
        ordered=ordered,
        no_decision=len(free) - len(ordered),
        newly_due=sum(1 for due in free if previous_run is None or as_utc(due.expires_at) > previous_run),
        due_waiting=len(waiting),
        headroom=headroom,
        breaker_reason=reason,
        deferred=max(0, len(ordered) - headroom) if reason is None else 0,
    )


def breaker_label(reason: Optional[str], dry_run: bool) -> str:
    """The breaker's name for the snapshot: `ok` without a reason, else `would trip` in log-only mode and `tripped`."""
    if reason is None:
        return BREAKER_OK
    return BREAKER_WOULD_TRIP if dry_run else BREAKER_TRIPPED


def breaker_reason(
    stale_count: int, pool: int, last_review_at: Optional[datetime], now: datetime, interval: int
) -> Optional[str]:
    """Why the janitor must stale nothing for an account, or None when it may.

    Trips when the account has stale facts and its last successful review is older than `interval` days + 72 h (or there
    is none), or when the pool (stale facts plus the facts of open questions) is over 50. The edges are exclusive:
    exactly `interval` days + 72 h and exactly 50 facts do not trip.
    """
    limit = timedelta(days=interval) + BREAKER_REVIEW_AGE
    if stale_count > 0 and (last_review_at is None or now - last_review_at > limit):
        return f"{stale_count} stale facts and no successful review in the last {int(limit.total_seconds() // 3600)} h"
    if pool > BREAKER_BACKLOG:
        return f"pool of {pool} (stale facts and facts waiting for an answer) is over {BREAKER_BACKLOG}"
    return None


def stale_headroom(pool: int) -> int:
    """How many more facts one run may stale for an account whose pool (stale facts plus the facts of open questions) is
    `pool`: the cap (BREAKER_BACKLOG) minus the pool before the run, never below 0."""
    return max(0, BREAKER_BACKLOG - pool)


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
    return not is_off_value(raw)


def daily_line(
    run_at: datetime, rows: Sequence[FactLifecycleDay], dry_run: bool, waiting: Optional[Mapping[str, int]] = None
) -> Optional[str]:
    """The day's ops line for the run at `run_at`: one line per account with something to say, or None when no row has.

    `expiry_tool_errors` of a row is the figure the caller chose to show (the service passes yesterday's). `waiting`
    gives, by account id, the due facts the run left to their open questions (`SweepPlan.due_waiting`); it is a figure of
    the run, not of the stored row.
    """
    held = waiting or {}
    lines = [
        _account_line(row, held.get(row.account_id, 0))
        for row in sorted(rows, key=lambda r: r.account_id) if _worth_a_line(row, held.get(row.account_id, 0))
    ]
    if not lines:
        return None
    moment = run_at.astimezone(timezone.utc) if run_at.tzinfo is not None else run_at
    header = f"🧹 Fact lifecycle — run {moment:%Y-%m-%d %H:%M} UTC" + (" · log-only" if dry_run else "")
    return "\n".join([header, *lines])


def _worth_a_line(row: FactLifecycleDay, waiting: int) -> bool:
    numbers = (row.due, row.newly_due, row.no_decision, row.staled, row.would_stale, row.deferred,
               row.stale_conflicts, row.backlog, row.expiry_tool_errors, waiting)
    return any(numbers) or row.breaker not in ("", BREAKER_OK)


def _account_line(row: FactLifecycleDay, waiting: int) -> str:
    oldest = f" (oldest {row.oldest_stale_days}d)" if row.oldest_stale_days is not None else ""
    waits = f" | due_waiting {waiting}" if waiting else ""
    return (
        f"{row.account_id[:8]}: due {row.due} (new {row.newly_due}, no decision {row.no_decision})"
        f" | staled {row.staled} / would stale {row.would_stale} / deferred {row.deferred}"
        f" | conflicts {row.stale_conflicts}"
        f" | breaker {row.breaker or BREAKER_OK}"
        f" | backlog {row.backlog}{oldest}"
        f" | expiry tool errors yesterday {row.expiry_tool_errors}"
        f"{waits}"
    )
