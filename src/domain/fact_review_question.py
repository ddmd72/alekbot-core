"""One memory-review question: a fact the review could not decide, asked of the owner one message at a time."""
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .fact_review_answers import AnswerKind
from .messaging import MessageRef

# A sent question waits this long from `asked_at`, answered or not; a queued question never times out.
QUESTION_TIMEOUT = timedelta(days=14)
# What the answers in words may add up to on one question (the review reads them once).
MAX_REPLY_CHARS = 2000

# What happens to the fact when nobody answers.
QuestionFallback = Literal["revive", "archive"]
# Where an answer came from.
AnswerSource = Literal["digit", "button", "cabinet", "review"]


class QuestionStatus(str, Enum):
    QUEUED = "queued"  # waiting in the queue, not sent
    PENDING = "pending"  # claimed to be sent, the post not confirmed
    ASKED = "asked"  # the owner has the message; it is the one open question
    UNANSWERED = "unanswered"  # asked on an earlier day, no answer; still answerable and still timing out
    CLOSED = "closed"


class ClosedBy(str, Enum):
    REVIEW = "review"
    TIMEOUT = "timeout"
    OWNER = "owner"
    RESOLVED_ELSEWHERE = "resolved_elsewhere"


_OPEN = frozenset({QuestionStatus.QUEUED, QuestionStatus.PENDING, QuestionStatus.ASKED, QuestionStatus.UNANSWERED})
_TIMES_OUT = frozenset({QuestionStatus.ASKED, QuestionStatus.UNANSWERED})


class FactReviewQuestion(BaseModel):
    """About exactly one fact. `quoted_text` is the fact's text when it was queued (what the owner is shown); `queue_key`
    orders the queue (the fact's `stale_since`, oldest first). `message_ref` is where the question was posted, which is
    how a reply is matched to it. `reply_text` collects the owner's answers in words until the review has read them
    (`offered_at`). `purge_after` exists only on a closed question."""

    model_config = ConfigDict(frozen=True)

    question_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    quoted_text: str
    fallback: QuestionFallback
    fallback_date: Optional[date] = None
    noise: bool = False
    status: QuestionStatus = QuestionStatus.QUEUED
    queue_key: datetime
    queued_at: datetime
    session_id: Optional[str] = None
    appended_at: Optional[datetime] = None
    asked_at: Optional[datetime] = None
    message_ref: Optional[MessageRef] = None
    reply_text: Optional[str] = Field(default=None, max_length=MAX_REPLY_CHARS)
    reply_at: Optional[datetime] = None
    offered_at: Optional[datetime] = None
    answer: Optional[AnswerKind] = None
    answer_source: Optional[AnswerSource] = None
    closed_at: Optional[datetime] = None
    closed_by: Optional[ClosedBy] = None
    purge_after: Optional[datetime] = None

    @model_validator(mode="after")
    def _status_and_its_fields_agree(self) -> "FactReviewQuestion":
        if self.status in _TIMES_OUT and self.asked_at is None:
            raise ValueError("an asked or unanswered question has an asked_at")
        if self.status is QuestionStatus.CLOSED:
            if self.closed_at is None or self.closed_by is None:
                raise ValueError("a closed question has closed_at and closed_by")
        elif any(v is not None for v in (self.closed_at, self.closed_by, self.purge_after, self.answer, self.answer_source)):
            raise ValueError("only a closed question has an answer, closed_at, closed_by or purge_after")
        return self

    @property
    def is_open(self) -> bool:
        """Its fact is in the review's hands: queued, being sent, sent, or sent and unanswered."""
        return self.status in _OPEN

    @property
    def reply_not_shown(self) -> bool:
        """An answer in words was recorded and the review has not read it yet."""
        return self.is_open and bool(self.reply_text) and self.offered_at is None

    def is_timed_out(self, now: datetime) -> bool:
        return self.status in _TIMES_OUT and self.asked_at is not None and self.asked_at + QUESTION_TIMEOUT <= now
