"""The janitor's 🚨 alerts (Facts S4 plan §3.11): which conditions an account's run reports, when a repeat is held back,
and how the messages read. One message per account per run, whatever the number of conditions; one more for the whole
run when the store holds closed records past their expiry. Pure: the service reads and posts, this decides and words."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Iterable, List, Optional, Tuple

from .fact_review_question import FactReviewQuestion, QuestionStatus
from .fact_review_rules import POOL_CAP, QUEUE_ALERT, QueueAlertAction, queue_alert
from .fact_review_state import FactReviewState
from .fact_write_context import as_utc

OLDEST_STALE_GRACE: Final[timedelta] = timedelta(days=2)       # the oldest stale fact may be this much older than the interval
OLDEST_ALERT_REPEAT: Final[timedelta] = timedelta(days=7)      # while it stays so, the alert repeats this often
UNSUCCESSFUL_REVIEW_ALERT: Final[timedelta] = timedelta(hours=48)   # reviews asked for and not once successful this long
EXPIRY_TOOL_ERRORS_ALERT: Final[int] = 10                      # yesterday's refused expiry calls above this raise the alert


@dataclass(frozen=True)
class AccountAlerts:
    """What the review's conditions say for one account's run: the lines to post, whether to record the oldest-stale
    alert (`oldest_alert_at = now`) or re-arm it (clear the mark), and what to do with the queue alert's mark (`raise`:
    set it to now, `rearm`: clear it)."""

    lines: Tuple[str, ...] = ()
    record_oldest: bool = False
    rearm_oldest: bool = False
    queue: Optional[QueueAlertAction] = None


def review_alerts(
    *,
    pool: int,
    oldest_stale: Optional[datetime],
    interval: int,
    state: FactReviewState,
    questions: Iterable[FactReviewQuestion],
    reply_intake_failed: int,
    now: datetime,
) -> AccountAlerts:
    """The memory review's alert conditions for one account.

    - the pool (stale facts plus the facts of open questions) is over `POOL_CAP`: every run while it is;
    - the oldest stale fact is older than `interval` days + `OLDEST_STALE_GRACE`: when it starts, then every
      `OLDEST_ALERT_REPEAT` (`state.oldest_alert_at`); a stale fact without `stale_since` has no age and raises none.
      When the condition stops holding the mark is cleared (`rearm_oldest`), so the next episode alerts at once;
    - more than `QUEUE_ALERT` questions are queued: once, until they are `QUEUE_ALERT` or fewer again
      (`queue_alert`, `state.queue_alert_at`);
    - no review has succeeded for more than `UNSUCCESSFUL_REVIEW_ALERT` since the first one asked for
      (`state.unsuccessful_since`): every run while it is;
    - `reply_intake_failed` (yesterday's figure) is above zero.

    All edges are exclusive: exactly at a limit nothing is raised.
    """
    held = list(questions)
    lines: List[str] = []
    if pool > POOL_CAP:
        lines.append(f"the pool is {pool}: stale facts and facts waiting for an answer, over {POOL_CAP}")
    record_oldest = False
    limit = timedelta(days=interval) + OLDEST_STALE_GRACE
    age = None if oldest_stale is None else as_utc(now) - as_utc(oldest_stale)
    holds = age is not None and age > limit
    if age is not None and holds and (state.oldest_alert_at is None or as_utc(now) - state.oldest_alert_at >= OLDEST_ALERT_REPEAT):
        lines.append(f"the oldest stale fact has waited {age.days} days (limit {limit.days})")
        record_oldest = True
    rearm_oldest = not holds and state.oldest_alert_at is not None
    queue = queue_alert(state, held)
    if queue == "raise":
        queued = sum(1 for q in held if q.status is QuestionStatus.QUEUED)
        lines.append(f"{queued} questions are queued, over {QUEUE_ALERT}")
    if state.unsuccessful_since is not None and as_utc(now) - state.unsuccessful_since > UNSUCCESSFUL_REVIEW_ALERT:
        hours = int((as_utc(now) - state.unsuccessful_since).total_seconds() // 3600)
        lines.append(f"no successful review for {hours} h")
    if reply_intake_failed > 0:
        lines.append(f"{reply_intake_failed} owner replies were not recorded yesterday")
    return AccountAlerts(tuple(lines), record_oldest, rearm_oldest, queue)


def expiry_errors_alert(expiry_tool_errors: int) -> Optional[str]:
    """The line for yesterday's refused expiry calls, when there were more than `EXPIRY_TOOL_ERRORS_ALERT`."""
    if expiry_tool_errors > EXPIRY_TOOL_ERRORS_ALERT:
        return f"{expiry_tool_errors} expiry tool calls were refused yesterday"
    return None


def breaker_alert(account_id: str, reason: str, held: int) -> str:
    """The line for a tripped breaker that held back `held` due facts."""
    return f"breaker tripped for account {account_id[:8]}: {reason}. {held} due facts were not staled."


def account_message(account_id: str, breaker: Optional[str], lines: Iterable[str]) -> Optional[str]:
    """The one 🚨 message of an account's run, or None when it has nothing to report. The breaker alone keeps the sentence
    it always had; with other conditions it comes first and they follow as items."""
    items = list(lines)
    if breaker is not None and not items:
        return f"🚨 Fact lifecycle {breaker}"
    if breaker is not None:
        items.insert(0, breaker)
    if not items:
        return None
    return "\n".join([f"🚨 Fact lifecycle, account {account_id[:8]}:", *(f"- {item}" for item in items)])


def purge_alert(overdue: int) -> str:
    """The run-level message for closed questions and digest claims still stored past their expiry."""
    return f"🚨 Fact lifecycle: {overdue} closed review records are still stored past their expiry."
