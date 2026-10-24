"""Builders for the memory-review rule tests: a stale or current fact, a question in each status, a review state."""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.domain.entities import ExpiryKind, FactEntity, FactState, FactType
from src.domain.fact_review_question import ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.fact_review_state import FactReviewState
from src.domain.messaging import MessageRef

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
REF = MessageRef(platform="slack", channel_id="D1", message_id="1729760000.000100")


def fact(fact_id: str = "f1", **over: Any) -> FactEntity:
    """A stale horizon fact reported 60 days before NOW with a 14-day horizon, unless `over` says otherwise."""
    reported = over.pop("reported_date", NOW - timedelta(days=60))
    fields: dict[str, Any] = dict(
        id=fact_id, account_id="acc", created_by_user_id="u1", lineage_id=f"L-{fact_id}", text="Takes vitamin D",
        type=FactType.STATE, state=FactState.STALE, is_current=True, reported_date=reported,
        expiry_kind=ExpiryKind.HORIZON, ttl_days=14, expires_at=reported + timedelta(days=14),
        stale_since=NOW - timedelta(days=10),
    )
    fields.update(over)
    return FactEntity(**fields)


def question(status: QuestionStatus = QuestionStatus.QUEUED, **over: Any) -> FactReviewQuestion:
    """A question in `status`, with exactly the fields that status needs."""
    asked_at = over.pop("asked_at", NOW - timedelta(days=3))
    fields: dict[str, Any] = dict(
        question_id="q1", account_id="acc", user_id="u1", fact_id="f1", quoted_text="Takes vitamin D",
        fallback="revive", queue_key=NOW - timedelta(days=30), queued_at=NOW - timedelta(days=4),
    )
    if status is not QuestionStatus.QUEUED:
        fields.update(session_id="u1:D1", appended_at=asked_at - timedelta(minutes=1), status=status)
    if status in (QuestionStatus.ASKED, QuestionStatus.UNANSWERED, QuestionStatus.CLOSED):
        fields.update(message_ref=REF, asked_at=asked_at)
    if status is QuestionStatus.PENDING and over.pop("posted", False):
        fields.update(message_ref=REF)
    fields.update(over)
    if status is QuestionStatus.CLOSED:
        fields.update(status=QuestionStatus.ASKED)
        return FactReviewQuestion(**fields).closed(ClosedBy.RESOLVED_ELSEWHERE, NOW)
    return FactReviewQuestion(**fields)


def state(**over: Any) -> FactReviewState:
    return FactReviewState(account_id="acc", **over)


def stale_since(days_ago: Optional[float]) -> Optional[datetime]:
    return None if days_ago is None else NOW - timedelta(days=days_ago)
