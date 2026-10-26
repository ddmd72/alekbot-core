"""The `fact_review` worker task: its type, its two modes, its payload and its Cloud Tasks dedup keys (Facts S4 plan §3.2
step 4, §3.7).

`daily` is a whole review run; `next` only sends the next question after the current one was closed. A deferred run is
one task per account and lease-retry bucket, so concurrent losers share it; a release is one task per closed question, so
a retried close never sends twice.
"""
from datetime import datetime
from typing import Any, Dict, Final, Literal

from .local_time import local_date
from .memory_write_lease import defer_bucket

FACT_REVIEW_TASK: Final[str] = "fact_review"

ReviewMode = Literal["daily", "next"]
REVIEW_MODES: Final[frozenset[str]] = frozenset({"daily", "next"})


def review_payload(user_id: str, account_id: str, mode: ReviewMode) -> Dict[str, Any]:
    """The task's body besides its type."""
    return {"user_id": user_id, "account_id": account_id, "mode": mode}


def defer_key(account_id: str, now: datetime) -> str:
    """The dedup key of a run deferred at `now` because the account's memory-write lease was held."""
    return f"fact-review-defer:{account_id}:{defer_bucket(now)}"


def next_key(question_id: str) -> str:
    """The dedup key of the send released by closing the current question `question_id`."""
    return f"fact-review-next:{question_id}"


def daily_key(account_id: str, now: datetime, tz: str) -> str:
    """The dedup key of the janitor's daily review for `account_id`: one per owner-local calendar day (`tz` is the owner's
    zone), so a second run the same local day names the same task and a run after local midnight a new one."""
    return f"fact-review:{account_id}:{local_date(now, tz).isoformat()}"
