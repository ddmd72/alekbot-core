"""
FactLifecycleSweepService — the janitor (FACT_LIFECYCLE_RFC D4, D10; S3 plan §3.14-§3.17).

Once a day: every current fact whose `expires_at` has passed becomes stale (in place, never archived or deleted, text never
read), unless the account's breaker is tripped. In log-only mode (`dry_run`) nothing is written to the facts: the run counts
what it would stale and reports the breaker. The numbers of each account are stored as one snapshot of the run, and one ops
line per day is posted after the run.

With the memory review wired (S4) the run also: visits every account the review has anything to do with; leaves the due
facts of open questions to the review; counts the pool (stale facts plus the facts of open questions) for the cap and the
breaker; enqueues the account's daily review task when `review_due` says so (log-only mode too); and reports the closed
questions the store's own expiry has not removed. Without a review store (S3 behaviour) none of that applies.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.due_fact import DueFact
from ..domain.exceptions import FactWriteConflict
from ..domain.fact_lifecycle_alerts import (
    account_message, breaker_alert, expiry_errors_alert, purge_alert, review_alerts,
)
from ..domain.fact_lifecycle_day import REVIEW_FIGURES, CounterField, FactLifecycleDay, SweepSnapshot
from ..domain.fact_lifecycle_review_line import QuestionStanding, question_standing
from ..domain.fact_lifecycle_run_guard import run_guard_until
from ..domain.fact_lifecycle_sweep import (
    BREAKER_OK, PURGE_OVERDUE_AFTER, breaker_label, daily_line, plan_sweep, stale_fields,
)
from ..domain.fact_review_question import FactReviewQuestion
from ..domain.fact_review_rules import open_fact_ids, pool, review_due, review_interval
from ..domain.fact_review_state import FactReviewState
from ..domain.fact_review_task_keys import FACT_REVIEW_TASK, daily_key, review_payload
from ..domain.fact_write_context import as_utc
from ..domain.owner_timezone import ResolvedOwnerZone
from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
from ..ports.repository import FactRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .owner_timezone_resolver import OwnerTimezoneResolver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactLifecycleSweepService:
    """The janitor. The memory review's collaborators (`review_store`, `owner_zones`, `account_repo`, `user_repo`) come
    together or not at all: without them the run is the S3 janitor (no questions, no review task). `task_queue` may be
    None while the review is wired (the queue is optional in the composition): the run then enqueues nothing."""

    OPS_POST_TIMEOUT_S = 10.0     # cap of every ops post: a hung sink holds neither a run, nor its error, nor a cancellation
    REVIEW_DEADLINE_S = 1800      # Cloud Tasks dispatch deadline of the daily review: a pass may run for most of it

    def __init__(
        self,
        repo: FactRepository,
        counters: FactLifecycleCountersPort,
        alert_sink: Optional[AlertSinkPort],
        dry_run: bool,
        clock: Callable[[], datetime] = _utcnow,
        *,
        review_store: Optional[FactReviewStore] = None,
        task_queue: Optional[TaskQueue] = None,
        owner_zones: "Optional[OwnerTimezoneResolver]" = None,
        account_repo: Optional[AccountRepository] = None,
        user_repo: Optional[UserRepository] = None,
    ) -> None:
        if review_store is not None and None in (owner_zones, account_repo, user_repo):
            raise ValueError("a janitor with a review store needs owner_zones, account_repo and user_repo")
        self._repo = repo
        self._counters = counters
        self._alert_sink = alert_sink
        self._dry_run = dry_run
        self._clock = clock
        self._review_store = review_store
        self._task_queue = task_queue
        self._owner_zones = owner_zones
        self._account_repo = account_repo
        self._user_repo = user_repo

    async def sweep(self) -> Dict[str, Any]:
        """Run the janitor once. Returns the totals.

        Any failure other than a write conflict (a query, a counters read or write, a stale write) stops the run: it is
        logged with its type and the stage reached, one 🚨 goes to the sink (best effort), and the error is re-raised.
        `breaker_tripped` counts `would trip` accounts in log-only mode too; `dry_run` is in the same dict.
        A run that finds the run guard held by another run does nothing and returns `{"status": "busy"}`.
        """
        progress: Dict[str, str] = {"stage": "start", "account": ""}
        try:
            return await self._run(progress)
        except Exception as error:
            where = progress["stage"] + (f" of account {progress['account'][:8]}" if progress["account"] else "")
            logger.error(f"[FactLifecycle] the run failed ({type(error).__name__}) at {where}", exc_info=True)
            await self._post(
                f"🚨 Fact lifecycle run failed ({type(error).__name__}) at {where}. Nothing after it ran."
            )
            raise

    async def _run(self, progress: Dict[str, str]) -> Dict[str, Any]:
        """Take the run guard before anything is read; hold it for the run and free it after, whatever happens."""
        now = as_utc(self._clock())
        owner = uuid.uuid4().hex
        progress["stage"] = "the run guard"
        if not await self._counters.acquire_run(owner, now, run_guard_until(now)):
            logger.info("[FactLifecycle] another run holds the run guard; this run does nothing")
            return {"status": "busy"}
        try:
            return await self._sweep_all(progress, now)
        finally:
            await self._release_guard(owner)

    async def _release_guard(self, owner: str) -> None:
        """A release error is logged, never raised: it must not hide the run's own result or error."""
        try:
            await self._counters.release_run(owner)
        except Exception as error:
            logger.warning(f"[FactLifecycle] the run guard was not released ({type(error).__name__}); it expires on its own")

    async def _sweep_all(self, progress: Dict[str, str], now: datetime) -> Dict[str, Any]:
        day = now.date()
        progress["stage"] = "the due query"
        due_by_account = _group(await self._repo.find_due_facts(now))
        progress["stage"] = "the stale backlog query"
        backlog = await self._repo.stale_backlog()
        progress["stage"] = "the review store read"
        questions_by_account, states = await self._review_reads()
        progress["stage"] = "the counters read"
        yesterday = {row.account_id: row for row in await self._counters.for_day(day - timedelta(days=1))}
        today = {row.account_id: row for row in await self._counters.for_day(day)}
        previous_runs = _previous_run_times(today, yesterday)
        accounts = sorted(
            set(due_by_account) | set(backlog) | set(states) | set(questions_by_account)
            | {row.account_id for row in yesterday.values() if row.has_review_figures()}
        )

        totals = {"accounts": 0, "due": 0, "staled": 0, "would_stale": 0, "deferred": 0, "conflicts": 0,
                  "no_decision": 0, "breaker_tripped": 0, "review_enqueued": 0}
        rows: List[FactLifecycleDay] = []
        waiting: Dict[str, int] = {}
        standing: Dict[str, QuestionStanding] = {}
        shown = (CounterField.EXPIRY_TOOL_ERRORS, *REVIEW_FIGURES)
        for account_id in accounts:
            progress["stage"], progress["account"] = "the account sweep", account_id
            snapshot, due_waiting = await self._sweep_account(
                account_id, day, now, due_by_account.get(account_id, []), backlog.get(account_id, (0, None)),
                previous_runs.get(account_id), states.get(account_id), questions_by_account.get(account_id, []),
                yesterday.get(account_id),
            )
            waiting[account_id] = due_waiting
            standing[account_id] = question_standing(questions_by_account.get(account_id, []))
            prior = yesterday.get(account_id)
            rows.append(FactLifecycleDay.of(account_id, day, snapshot, prior.counts(shown) if prior else {}))
            totals["accounts"] += 1
            totals["due"] += snapshot.due
            totals["staled"] += snapshot.staled
            totals["would_stale"] += snapshot.would_stale
            totals["deferred"] += snapshot.deferred
            totals["conflicts"] += snapshot.stale_conflicts
            totals["no_decision"] += snapshot.no_decision
            totals["breaker_tripped"] += int(snapshot.breaker != BREAKER_OK)
            totals["review_enqueued"] += snapshot.review_enqueued
        progress["stage"], progress["account"] = "the purge check", ""
        purge_overdue = await self._purge_overdue(now)
        if purge_overdue:
            await self._post(purge_alert(purge_overdue))
        progress["stage"] = "the ops line"
        seen = set(accounts)
        rows.extend(
            FactLifecycleDay(account_id=row.account_id, day=day, expiry_tool_errors=row.expiry_tool_errors)
            for row in yesterday.values() if row.account_id not in seen and row.expiry_tool_errors
        )
        await self._post_line(now, day, rows, waiting, standing, purge_overdue)
        return {**totals, "purge_overdue": purge_overdue, "dry_run": self._dry_run}

    async def _review_reads(self) -> Tuple[Dict[str, List[FactReviewQuestion]], Dict[str, FactReviewState]]:
        """Every open question by account, and every account's review state. Both empty without a review store."""
        if self._review_store is None:
            return {}, {}
        by_account: Dict[str, List[FactReviewQuestion]] = {}
        for question in await self._review_store.all_open():
            by_account.setdefault(question.account_id, []).append(question)
        return by_account, {state.account_id: state for state in await self._review_store.all_states()}

    async def _purge_overdue(self, now: datetime) -> int:
        """Closed questions and digest claims still stored past their expiry by more than `PURGE_OVERDUE_AFTER`: a run-level
        figure for the result and the ops line, never part of an account's snapshot."""
        if self._review_store is None:
            return 0
        return await self._review_store.purge_overdue(now - PURGE_OVERDUE_AFTER)

    async def _sweep_account(
        self,
        account_id: str,
        day: date,
        now: datetime,
        dues: List[DueFact],
        backlog: Tuple[int, Optional[datetime]],
        previous_run: Optional[datetime],
        review_state: Optional[FactReviewState],
        questions: Sequence[FactReviewQuestion],
        yesterday: Optional[FactLifecycleDay] = None,
    ) -> Tuple[SweepSnapshot, int]:
        """One account's run. Returns its snapshot and the due facts it left to their open questions (a figure of the
        run: the ops line shows it, the snapshot does not). `yesterday` is the account's row of the day before, whose
        figures the alerts read. The run's alerts go out as one 🚨 after the review is enqueued."""
        stale_count, oldest = backlog
        state = review_state or FactReviewState(account_id=account_id)
        owner = await self._owner(account_id)
        interval = await self._interval(account_id, owner)
        held = pool(stale_count, questions)
        plan = plan_sweep(
            dues, stale_count, open_fact_ids(questions), previous_run, now,
            pool=held, last_review_at=state.last_review_at, interval=interval,
        )
        reason = plan.breaker_reason
        staled = conflicts = would_stale = deferred = 0
        breaker_line: Optional[str] = None

        if self._dry_run:
            would_stale = len(plan.ordered)
            deferred = plan.deferred
        elif reason is None:
            staled, conflicts, deferred = await self._stale(account_id, now, plan.ordered, plan.headroom)
        else:
            breaker_line = self._hold_back(account_id, reason, len(plan.ordered))

        enqueued = await self._enqueue_review(account_id, owner, state, questions, interval, stale_count + staled, now)
        await self._alert(account_id, now, breaker_line, held, oldest, interval, state, questions, yesterday)
        breaker = breaker_label(reason, self._dry_run)
        snapshot = SweepSnapshot(
            run_at=now,
            due=len(dues) - plan.due_waiting,
            newly_due=plan.newly_due,
            no_decision=plan.no_decision,
            staled=staled,
            would_stale=would_stale,
            deferred=deferred,
            stale_conflicts=conflicts,
            breaker=breaker,
            backlog=stale_count,
            oldest_stale_days=max(0, (now - as_utc(oldest)).days) if oldest is not None else None,
            review_enqueued=enqueued,
        )
        # Written in log-only mode too: the numbers are what the owner reads before the janitor is switched on.
        await self._counters.record_sweep(account_id, day, snapshot)
        logger.info(
            f"[FactLifecycle] account={account_id[:8]} due={snapshot.due} newly_due={plan.newly_due} "
            f"no_decision={snapshot.no_decision} staled={staled} would_stale={would_stale} deferred={deferred} conflicts={conflicts} "
            f"backlog={stale_count} breaker={reason or BREAKER_OK} review_enqueued={snapshot.review_enqueued} dry_run={self._dry_run}"
        )
        return snapshot, plan.due_waiting

    async def _owner(self, account_id: str) -> Optional[ResolvedOwnerZone]:
        """The account's first owner and that owner's zone; None while the review is not wired."""
        if self._review_store is None or self._owner_zones is None:
            return None
        return await self._owner_zones.resolve(account_id)

    async def _interval(self, account_id: str, owner: Optional[ResolvedOwnerZone]) -> int:
        """Days between the account's reviews: the owner's setting, else the account default, else the system default
        (`review_interval`). Read only while the review is wired."""
        if self._account_repo is None or self._user_repo is None:
            return review_interval(None, None)
        account = await self._account_repo.get_account(account_id)
        profile = await self._user_repo.get_user(owner.owner_user_id) if owner and owner.owner_user_id else None
        return review_interval(
            profile.config if profile is not None else None, account.account_defaults if account is not None else None
        )

    async def _enqueue_review(
        self,
        account_id: str,
        owner: Optional[ResolvedOwnerZone],
        state: FactReviewState,
        questions: Sequence[FactReviewQuestion],
        interval: int,
        stale_after_run: int,
        now: datetime,
    ) -> int:
        """Enqueue the account's daily review when `review_due` gives a reason, in log-only mode too. Returns 1 when the
        day's task exists after the call (a run that finds the task of the owner's local day already queued counts: the
        review is requested), else 0. No owner, or no queue: a WARNING and 0."""
        if self._review_store is None or owner is None:
            return 0
        reason = review_due(stale_after_run, state, questions, interval, now, owner.zone)
        if reason is None:
            return 0
        if owner.owner_user_id is None:
            logger.warning(f"[FactLifecycle] review due for account {account_id[:8]} ({reason}) but it has no owner; not enqueued")
            return 0
        if self._task_queue is None:
            logger.warning(f"[FactLifecycle] review due for account {account_id[:8]} ({reason}) but there is no task queue; not enqueued")
            return 0
        created = await self._task_queue.enqueue_worker_task(
            FACT_REVIEW_TASK,
            review_payload(owner.owner_user_id, account_id, "daily"),
            deadline_seconds=self.REVIEW_DEADLINE_S,
            dedup_key=daily_key(account_id, now, owner.zone),
        )
        await self._review_store.mark_enqueued(account_id, now)
        logger.info(
            f"[FactLifecycle] review {'enqueued' if created else 'already queued today'} for account {account_id[:8]} ({reason})"
        )
        return 1

    async def _stale(
        self, account_id: str, now: datetime, decided: Sequence[DueFact], headroom: int
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
                    await self._repo.stale_due_fact(due, stale_fields(due, now))
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

    def _hold_back(self, account_id: str, reason: str, held: int) -> Optional[str]:
        """The breaker is tripped in write mode: log it; the alert line is returned only when a due fact with a decision was
        actually held back (the snapshot and the ops line report a tripped breaker either way)."""
        logger.warning(f"[FactLifecycle] breaker tripped for account {account_id[:8]}: {reason}; {held} due facts not staled")
        return breaker_alert(account_id, reason, held) if held else None

    async def _alert(
        self,
        account_id: str,
        now: datetime,
        breaker_line: Optional[str],
        held: int,
        oldest: Optional[datetime],
        interval: int,
        state: FactReviewState,
        questions: Sequence[FactReviewQuestion],
        yesterday: Optional[FactLifecycleDay],
    ) -> None:
        """The account's one 🚨 of the run (log-only mode too): the breaker, when it held facts back, and the conditions of
        `review_alerts` and `expiry_errors_alert`. The marks the repeat rules need are written before the post, so a lost
        post is not repeated. The review's conditions need its store; without one only the breaker and the expiry
        errors can be reported."""
        lines: List[str] = []
        expiry = expiry_errors_alert(yesterday.expiry_tool_errors if yesterday else 0)
        if expiry is not None:
            lines.append(expiry)
        if self._review_store is not None:
            found = review_alerts(
                pool=held, oldest_stale=oldest, interval=interval, state=state, questions=questions,
                reply_intake_failed=yesterday.reply_intake_failed if yesterday else 0, now=now,
            )
            lines = [*found.lines, *lines]
            if found.record_oldest:
                await self._mark(account_id, "oldest alert", self._review_store.mark_oldest_alert(account_id, now))
            if found.queue is not None:
                await self._mark(
                    account_id, "queue alert",
                    self._review_store.mark_queue_alert(account_id, now if found.queue == "raise" else None),
                )
        message = account_message(account_id, breaker_line, lines)
        if message is not None:
            await self._post(message)

    @staticmethod
    async def _mark(account_id: str, what: str, write: Awaitable[bool]) -> None:
        """A review-state mark; an account without a state document gives False, which only means the alert may repeat."""
        if not await write:
            logger.warning(f"[FactLifecycle] the {what} of account {account_id[:8]} was not recorded: no review state")

    async def _post_line(
        self,
        now: datetime,
        day: date,
        rows: List[FactLifecycleDay],
        waiting: Mapping[str, int],
        standing: Mapping[str, QuestionStanding],
        purge_overdue: int,
    ) -> None:
        """Post the day's line after the run, once a day. The day is claimed before the post: a failed post loses the
        day's line (the snapshots stay in the store), where claiming after could post it twice."""
        line = daily_line(now, rows, self._dry_run, waiting, standing=standing, purge_overdue=purge_overdue)
        if line is None or self._alert_sink is None:
            return
        try:
            if not await self._counters.claim_line(day):
                return
        except Exception as error:
            logger.warning(f"[FactLifecycle] the ops line was not claimed and is not posted ({type(error).__name__})")
            return
        await self._post(line)

    async def _post(self, text: str) -> None:
        """One ops message, best effort and capped: a failure or a timeout is a WARNING and the run goes on; a
        cancellation (not an Exception) propagates."""
        if self._alert_sink is None:
            return
        try:
            await asyncio.wait_for(self._alert_sink.post(text), timeout=self.OPS_POST_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(f"[FactLifecycle] an ops message was cut after {self.OPS_POST_TIMEOUT_S:g}s")
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

