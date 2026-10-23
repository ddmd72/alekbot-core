"""
FactLifecycleSweepService — the janitor (FACT_LIFECYCLE_RFC D4, D10; S3 plan §3.14-§3.17).

Once a day: every current fact whose `expires_at` has passed becomes stale (in place, never archived or deleted, text never
read), unless the account's breaker is tripped. In log-only mode (`dry_run`) nothing is written to the facts: the run counts
what it would stale and reports the breaker. The numbers of each account are stored as one snapshot of the run, and one ops
line per day is posted after the run. No review is enqueued in S3 (`review_enqueued=0`).
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ..domain.due_fact import DueFact
from ..domain.exceptions import FactWriteConflict
from ..domain.fact_lifecycle_day import FactLifecycleDay
from ..domain.fact_lifecycle_sweep import breaker_reason, daily_line, stale_headroom
from ..domain.fact_write_context import as_utc
from ..ports.alert_sink import AlertSinkPort
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.repository import FactRepository
from ..utils.logger import logger

_BREAKER_OK = "ok"
_BREAKER_TRIPPED = "tripped"
_BREAKER_WOULD_TRIP = "would trip"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactLifecycleSweepService:
    FAILURE_ALERT_TIMEOUT_S = 10.0     # a hung sink may not hold the original error (or a cancellation) longer

    def __init__(
        self,
        repo: FactRepository,
        counters: FactLifecycleCountersPort,
        alert_sink: Optional[AlertSinkPort],
        dry_run: bool,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._repo = repo
        self._counters = counters
        self._alert_sink = alert_sink
        self._dry_run = dry_run
        self._clock = clock

    async def sweep(self) -> Dict[str, Any]:
        """Run the janitor once. Returns the totals.

        Any failure other than a write conflict (a query, a counters read or write, a stale write) stops the run: it is
        logged with its type and the stage reached, one 🚨 goes to the sink (best effort), and the error is re-raised.
        `breaker_tripped` counts `would trip` accounts in log-only mode too; `dry_run` is in the same dict.
        """
        progress: Dict[str, str] = {"stage": "start", "account": ""}
        try:
            return await self._run(progress)
        except Exception as error:
            where = progress["stage"] + (f" of account {progress['account'][:8]}" if progress["account"] else "")
            logger.error(f"[FactLifecycle] the run failed ({type(error).__name__}) at {where}", exc_info=True)
            await self._post_failure(
                f"🚨 Fact lifecycle run failed ({type(error).__name__}) at {where}. Nothing after it ran."
            )
            raise

    async def _run(self, progress: Dict[str, str]) -> Dict[str, Any]:
        now = as_utc(self._clock())
        day = now.date()
        progress["stage"] = "the due query"
        due_by_account = _group(await self._repo.find_due_facts(now))
        progress["stage"] = "the stale backlog query"
        backlog = await self._repo.stale_backlog()
        accounts = sorted(set(due_by_account) | set(backlog))
        progress["stage"] = "the counters read"
        yesterday = {row.account_id: row for row in await self._counters.for_day(day - timedelta(days=1))}
        today = {row.account_id: row for row in await self._counters.for_day(day)}
        previous_runs = _previous_run_times(today, yesterday)

        totals = {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "deferred": 0, "conflicts": 0,
                  "no_decision": 0, "breaker_tripped": 0}
        rows: List[FactLifecycleDay] = []
        for account_id in accounts:
            progress["stage"], progress["account"] = "the account sweep", account_id
            snapshot = await self._sweep_account(
                account_id, day, now, due_by_account.get(account_id, []), backlog.get(account_id, (0, None)),
                previous_runs.get(account_id),
            )
            tool_errors = yesterday[account_id].expiry_tool_errors if account_id in yesterday else 0
            rows.append(_row(account_id, day, snapshot, tool_errors))
            totals["accounts"] += 1
            totals["due"] += snapshot["due"]
            totals["staled"] += snapshot["staled"]
            totals["would_stale"] += snapshot["would_stale"]
            totals["deferred"] += snapshot["deferred"]
            totals["conflicts"] += snapshot["stale_conflicts"]
            totals["no_decision"] += snapshot["no_decision"]
            totals["breaker_tripped"] += int(snapshot["breaker"] != _BREAKER_OK)
        progress["stage"], progress["account"] = "the ops line", ""
        seen = set(accounts)
        rows.extend(
            FactLifecycleDay(account_id=row.account_id, day=day, expiry_tool_errors=row.expiry_tool_errors)
            for row in yesterday.values() if row.account_id not in seen and row.expiry_tool_errors
        )
        await self._post_line(now, day, rows)
        return {**totals, "dry_run": self._dry_run}

    async def _sweep_account(
        self,
        account_id: str,
        day: date,
        now: datetime,
        dues: List[DueFact],
        backlog: Tuple[int, Optional[datetime]],
        previous_run: Optional[datetime],
    ) -> Dict[str, Any]:
        stale_count, oldest = backlog
        reason = breaker_reason(stale_count, None, now)      # S4 wires the last successful review
        # Oldest expiry first, so the cap defers the newest; the id makes the order total.
        decided = sorted((due for due in dues if due.expiry_kind is not None), key=lambda due: (as_utc(due.expires_at), due.fact_id))
        newly_due = sum(1 for due in dues if previous_run is None or as_utc(due.expires_at) > previous_run)
        headroom = stale_headroom(stale_count)
        staled = conflicts = would_stale = deferred = 0

        if self._dry_run:
            would_stale = len(decided)
            deferred = max(0, len(decided) - headroom) if reason is None else 0
        elif reason is None:
            staled, conflicts, deferred = await self._stale(account_id, now, decided, headroom)
        else:
            await self._hold_back(account_id, reason, len(decided))

        breaker = _BREAKER_OK if reason is None else (_BREAKER_WOULD_TRIP if self._dry_run else _BREAKER_TRIPPED)
        snapshot: Dict[str, Any] = {
            "run_at": now,
            "due": len(dues),
            "newly_due": newly_due,
            "no_decision": len(dues) - len(decided),
            "staled": staled,
            "would_stale": would_stale,
            "deferred": deferred,
            "stale_conflicts": conflicts,
            "breaker": breaker,
            "backlog": stale_count,
            "oldest_stale_days": max(0, (now - as_utc(oldest)).days) if oldest is not None else None,
        }
        # Written in log-only mode too: the numbers are what the owner reads before the janitor is switched on.
        await self._counters.record_sweep(account_id, day, snapshot)
        logger.info(
            f"[FactLifecycle] account={account_id[:8]} due={snapshot['due']} newly_due={newly_due} "
            f"no_decision={snapshot['no_decision']} staled={staled} would_stale={would_stale} deferred={deferred} conflicts={conflicts} "
            f"backlog={stale_count} breaker={reason or _BREAKER_OK} review_enqueued=0 dry_run={self._dry_run}"
        )
        return snapshot

    async def _stale(
        self, account_id: str, now: datetime, decided: List[DueFact], headroom: int
    ) -> Tuple[int, int, int]:
        """Stale `decided` (oldest first) until `headroom` writes succeeded. Returns (staled, conflicts, deferred): a
        conflict does not use headroom; the facts never tried are deferred to the next run."""
        staled = conflicts = attempted = 0
        try:
            for due in decided:
                if staled >= headroom:
                    break
                attempted += 1
                try:
                    await self._repo.stale_due_fact(due, now)
                except FactWriteConflict:
                    conflicts += 1
                    logger.info(f"[FactLifecycle] fact {due.fact_id[:8]} changed since it was read; skipped until the next run")
                except Exception as error:
                    logger.error(
                        f"[FactLifecycle] staling fact {due.fact_id[:8]} of account {account_id[:8]} failed "
                        f"({type(error).__name__}); the run stops", exc_info=True,
                    )
                    raise
                else:
                    staled += 1
        finally:
            if staled:
                await self._repo.refresh_biographical_context_cache(account_id)     # a staled fact leaves every prompt (D5)
        return staled, conflicts, len(decided) - attempted

    async def _hold_back(self, account_id: str, reason: str, held: int) -> None:
        """The breaker is tripped in write mode: log it; alert only when a due fact with a decision was actually held back
        (the snapshot and the ops line report a tripped breaker either way)."""
        logger.warning(f"[FactLifecycle] breaker tripped for account {account_id[:8]}: {reason}; {held} due facts not staled")
        if held:
            await self._post(
                f"🚨 Fact lifecycle breaker tripped for account {account_id[:8]}: {reason}. {held} due facts were not staled."
            )

    async def _post_line(self, now: datetime, day: date, rows: List[FactLifecycleDay]) -> None:
        """Post the day's line after the run, once a day. The day is claimed before the post: a failed post loses the
        day's line (the snapshots stay in the store), where claiming after could post it twice."""
        line = daily_line(now, rows, self._dry_run)
        if line is None or self._alert_sink is None:
            return
        try:
            if not await self._counters.claim_line(day):
                return
        except Exception as error:
            logger.warning(f"[FactLifecycle] the ops line was not claimed and is not posted ({type(error).__name__})")
            return
        await self._post(line)

    async def _post_failure(self, text: str) -> None:
        """The run-failed alert, capped: a timeout is a WARNING, the caller re-raises the original error, and a
        cancellation (not an Exception) propagates."""
        try:
            await asyncio.wait_for(self._post(text), timeout=self.FAILURE_ALERT_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"[FactLifecycle] the run-failed alert was cut after {self.FAILURE_ALERT_TIMEOUT_S:.0f}s")

    async def _post(self, text: str) -> None:
        if self._alert_sink is None:
            return
        try:
            await self._alert_sink.post(text)
        except Exception as error:
            logger.warning(f"[FactLifecycle] an ops message was not delivered ({type(error).__name__})")


def _previous_run_times(
    today: Mapping[str, FactLifecycleDay], yesterday: Mapping[str, FactLifecycleDay]
) -> Dict[str, datetime]:
    """Per account, the `run_at` of its previous snapshot: today's if it has one, else yesterday's."""
    found: Dict[str, datetime] = {}
    for rows in (yesterday, today):      # today's overwrites yesterday's
        for account_id, row in rows.items():
            if row.run_at is not None:
                found[account_id] = as_utc(row.run_at)
    return found


def _group(dues: List[DueFact]) -> Dict[str, List[DueFact]]:
    grouped: Dict[str, List[DueFact]] = {}
    for due in dues:
        grouped.setdefault(due.account_id, []).append(due)
    return grouped


def _row(account_id: str, day: date, snapshot: Mapping[str, Any], tool_errors: int) -> FactLifecycleDay:
    return FactLifecycleDay(
        account_id=account_id, day=day, run_at=snapshot["run_at"], due=snapshot["due"],
        newly_due=snapshot["newly_due"], no_decision=snapshot["no_decision"], staled=snapshot["staled"],
        would_stale=snapshot["would_stale"], deferred=snapshot["deferred"], stale_conflicts=snapshot["stale_conflicts"],
        breaker=snapshot["breaker"], backlog=snapshot["backlog"], oldest_stale_days=snapshot["oldest_stale_days"],
        expiry_tool_errors=tool_errors,
    )
