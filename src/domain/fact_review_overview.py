"""What the owner can look at of the memory review (Facts S4 plan §3.11, §3.18): which stored facts he may list, the facts
the review set aside lately, and his open questions in the order they are dealt with.

Pure: a reader supplies the stored facts and questions, and this module decides which of them are shown, in which order,
and under which status. Another account's or another user's question is never shown to a user."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, List, Literal, Mapping, Optional, Sequence

from .entities import FactEntity, FactState
from .fact_review_question import SENT_STATUSES, FactReviewQuestion, QuestionDelivery, QuestionKind
from .fact_review_rules import current_question, queue_order
from .fact_review_state import FactReviewState

# The states of the facts the owner can list: the ones he has, and the ones waiting for his word.
LISTABLE_STATES: Final[tuple[str, ...]] = (FactState.CURRENT.value, FactState.STALE.value)
# How far back the facts the review set aside are listed.
SET_ASIDE_WINDOW: Final[timedelta] = timedelta(days=30)
# How many of them are listed.
SET_ASIDE_LIMIT: Final[int] = 50

# Where an open question stands: the one out now, one sent and not answered, or one still in line.
QueuePlace = Literal["current", "sent", "waiting"]


@dataclass(frozen=True)
class QueueEntry:
    """One open question as the owner sees it: its fact in full as it stands now (the quoted text when the fact is gone),
    the fact's domain and `reported_date` when known, since when it has been waiting (`waiting_since`, the queue key: the
    fact's `stale_since`), when it was sent (`asked_at`, none while it waits), its place, and the question's kind and
    delivery (P2-3: a `digest` question is a routine invoice, answered in the Cabinet only)."""

    question_id: str
    fact_id: str
    text: str
    domain: Optional[str]
    reported_date: Optional[datetime]
    waiting_since: datetime
    asked_at: Optional[datetime]
    place: QueuePlace
    question: QuestionKind = "still_true"
    delivery: QuestionDelivery = "chat"


def listable_state(raw: Optional[str]) -> Optional[str]:
    """The state a list request names: none asked is `current`; `current` or `stale` as asked; anything else None."""
    state = FactState.CURRENT.value if raw is None or raw == "" else raw
    return state if state in LISTABLE_STATES else None


def set_aside_since(now: datetime) -> datetime:
    """Where the list of lately set-aside facts starts."""
    return now - SET_ASIDE_WINDOW


def user_questions(questions: Sequence[FactReviewQuestion], account_id: str, user_id: str) -> List[FactReviewQuestion]:
    """The open questions of `account_id` that were asked of `user_id`: a question shows its fact whole, so no one sees
    another account's, or another user's in the same account."""
    return [q for q in questions if q.is_open and q.asked_of(account_id, user_id)]


def holds_other_users_question(questions: Sequence[FactReviewQuestion], account_id: str, user_id: str) -> bool:
    """Whether any of `questions` belongs to another user of `account_id`: answering it is not `user_id`'s to do. A question
    of another account is not that: it does not exist for this account."""
    return any(q.account_id == account_id and not q.asked_of(account_id, user_id) for q in questions)


def queue_entries(
    state: Optional[FactReviewState],
    questions: Sequence[FactReviewQuestion],
    facts: Mapping[str, FactEntity],
    account_id: str,
) -> List[QueueEntry]:
    """The entries for `questions` (already `user_questions`) in the order they are dealt with: the question out now (the
    state's current one), then those sent and not answered, the longest unanswered first, then the queue in its order.

    `facts` holds each question's fact as it stands, by the question's `fact_id`; a question without one shows what it
    quoted."""
    pointer = state if state is not None else FactReviewState(account_id=account_id)
    current = current_question(pointer, questions)
    sent = sorted(
        (q for q in questions if q is not current and q.status in SENT_STATUSES),
        key=lambda q: (q.asked_at or q.queued_at, q.question_id),
    )
    waiting = queue_order(q for q in questions if q is not current and q.status not in SENT_STATUSES)
    entries = [_entry(q, facts.get(q.fact_id), "current") for q in ([current] if current is not None else [])]
    entries += [_entry(q, facts.get(q.fact_id), "sent") for q in sent]
    entries += [_entry(q, facts.get(q.fact_id), "waiting") for q in waiting]
    return entries


def _entry(question: FactReviewQuestion, fact: Optional[FactEntity], place: QueuePlace) -> QueueEntry:
    return QueueEntry(
        question_id=question.question_id,
        fact_id=question.fact_id,
        text=fact.text if fact is not None else question.quoted_text,
        domain=fact.domain.value if fact is not None and fact.domain is not None else None,
        reported_date=fact.reported_date if fact is not None else None,
        waiting_since=question.queue_key,
        asked_at=question.asked_at,
        place=place,
        question=question.question,
        delivery=question.delivery,
    )
