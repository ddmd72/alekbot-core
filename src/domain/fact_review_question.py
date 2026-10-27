"""One memory-review question: a fact the review could not decide, asked of the owner one message at a time.

Life of a question: `queued` -> (`claimed`) `pending` -> (`appended`, `posted`) -> `asked` -> `unanswered` -> `closed`.
Every move is a method here that returns a new, validated question or raises `QuestionTransitionError`; the store only
applies the result in its transaction. A `pending` question that has a message ref was delivered (`delivered`), whatever
happened to the process after the post: it is completed as `asked`, never sent again."""
from datetime import date, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, Final, Literal, Mapping, Optional, Self, Tuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from .exceptions import QuestionTransitionError
from .fact_lifecycle_day import CounterField
from .fact_review_answer_kind import AnswerKind
from .messaging import MessageRef

# A sent question waits this long from `asked_at`, answered or not; a queued question never times out.
QUESTION_TIMEOUT = timedelta(days=14)
# A closed question is kept this long after `closed_at`, for the Cabinet, the digest and the trail, then expires.
QUESTION_RETENTION = timedelta(days=90)
# What the owner's answers in words may add up to on one question (the review reads them once).
MAX_REPLY_CHARS = 2000

# The binding kind that routes a choice made on a question's message to the fact-question handler.
FACT_QUESTION_KIND = "fact_question"

# What happens to the fact when nobody answers: kept (`revive`), set aside (`archive`), or its `unknown` text written as
# history (`history`, P2-3).
QuestionFallback = Literal["revive", "archive", "history"]
# What a question asks (P2-3): whether the fact still holds, whether a plan took place, whether a validity end was renewed,
# whether a bill was paid. Each kind has its own options (`fact_review_options.OPTIONS`).
QuestionKind = Literal["still_true", "happened", "renewed", "paid"]
# Who asked: the review model (it wrote the texts), or code for a fact the review kept twice (code builds the texts).
QuestionOrigin = Literal["review", "kept_twice"]
# Where it is answered: the owner's chat, or only the Cabinet, listed in the weekly digest (a routine invoice).
QuestionDelivery = Literal["chat", "digest"]
# The text key of the `unknown` outcome: what "don't remember" and silence write as history.
UNKNOWN_TEXT: Final = "unknown"
# The text keys a `review` question carries, by (kind, delivery). A `kept_twice` question carries none.
TEXT_KEYS: Final[Mapping[Tuple[QuestionKind, QuestionDelivery], Tuple[str, ...]]] = MappingProxyType({
    ("still_true", "chat"): (),
    ("happened", "chat"): ("happened", "not_happened", UNKNOWN_TEXT),
    ("renewed", "chat"): ("renewed", "not_renewed", "not_needed", UNKNOWN_TEXT),
    ("paid", "chat"): ("paid", "not_paid", UNKNOWN_TEXT),
    ("paid", "digest"): ("paid", "not_paid"),
})
# An unanswered digest question closes `resolved_elsewhere` after this: its fact already says "payment not confirmed".
DIGEST_QUESTION_DAYS: Final = timedelta(days=90)
# Where an answer came from. `review` is the review applying the owner's words.
AnswerSource = Literal["digit", "button", "cabinet", "review"]

# The owner's answer sources, each with the day counter its answer adds to. A source outside this map is not the owner's.
ANSWER_COUNTERS: Mapping[str, CounterField] = MappingProxyType({
    "digit": CounterField.ANSWERED_BY_DIGIT,
    "button": CounterField.ANSWERED_BY_BUTTON,
    "cabinet": CounterField.ANSWERED_IN_CABINET,
})



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


# The statuses of a question the review still holds, and of one the owner has been shown.
OPEN_STATUSES = frozenset({QuestionStatus.QUEUED, QuestionStatus.PENDING, QuestionStatus.ASKED, QuestionStatus.UNANSWERED})
SENT_STATUSES = frozenset({QuestionStatus.ASKED, QuestionStatus.UNANSWERED})
_ATTEMPT_FIELDS = (
    "session_id", "appended_at", "message_ref", "asked_at", "reply_text", "reply_at", "offered_at",
)
_SENT_FIELDS = ("asked_at", "session_id", "appended_at", "message_ref")
_CLOSING_FIELDS = ("closed_at", "closed_by", "answer", "answer_source")


class FactReviewQuestion(BaseModel):
    """About exactly one fact. `quoted_text` is the fact's text when it was queued (what the owner is shown); `queue_key`
    orders the queue (the fact's `stale_since`, oldest first). `message_ref` is where the question was posted, which is
    how a reply is matched to it. `reply_text` collects the owner's answers in words until the review has read them
    (`offered_at`). Every datetime is timezone-aware.

    P2-3: `question` is its kind, `texts` the outcome texts the review wrote at ask time (a `kept_twice` question has
    none: code builds them), `delivery` chat or digest (Cabinet only), `fallback_attempts` how often its history fallback
    failed. `texts` makes it unhashable: a question never goes into a set or a dict key.

    Frozen, and `model_copy` validates again: a question that exists is a legal one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    fact_id: str = Field(min_length=1)
    quoted_text: str = Field(min_length=1)
    fallback: QuestionFallback
    fallback_date: Optional[date] = None
    noise: bool = False
    status: QuestionStatus = QuestionStatus.QUEUED
    queue_key: AwareDatetime
    queued_at: AwareDatetime
    session_id: Optional[str] = Field(default=None, min_length=1)
    appended_at: Optional[AwareDatetime] = None
    asked_at: Optional[AwareDatetime] = None
    message_ref: Optional[MessageRef] = None
    reply_text: Optional[str] = Field(default=None, min_length=1, max_length=MAX_REPLY_CHARS)
    reply_at: Optional[AwareDatetime] = None
    offered_at: Optional[AwareDatetime] = None
    answer: Optional[AnswerKind] = None
    answer_source: Optional[AnswerSource] = None
    closed_at: Optional[AwareDatetime] = None
    closed_by: Optional[ClosedBy] = None
    question: QuestionKind = "still_true"
    texts: Dict[str, str] = Field(default_factory=dict)
    origin: QuestionOrigin = "review"
    delivery: QuestionDelivery = "chat"
    fallback_attempts: int = Field(default=0, ge=0)

    # ------------------------------------------------------------------------------------------------ invariants

    @model_validator(mode="after")
    def _the_fields_agree_with_the_status(self) -> Self:
        if (self.fallback_date is not None or self.noise) and self.fallback != "archive":
            raise ValueError("a fallback date or noise belongs to an archive fallback")
        self._check_kind()
        self._check_attempt()
        self._check_reply()
        if self.status is QuestionStatus.CLOSED:
            self._check_closing()
        elif any(getattr(self, name) is not None for name in _CLOSING_FIELDS):
            raise ValueError("only a closed question has an answer, closed_at or closed_by")
        return self

    def _check_kind(self) -> None:
        """The kind, its texts, its origin, its delivery and its fallback agree (P2-3)."""
        if self.delivery == "digest" and (self.question != "paid" or self.origin != "review"):
            raise ValueError("a digest question is a paid question the review asked")
        if self.origin == "review":
            if set(self.texts) != set(TEXT_KEYS[(self.question, self.delivery)]):
                raise ValueError(f"a {self.question} question the review asked has exactly its texts")
            if any(not text.strip() for text in self.texts.values()):
                raise ValueError("a question's texts are not empty")
        elif self.texts or self.question not in ("still_true", "happened"):
            raise ValueError("a kept-twice question is still_true or happened and has no texts")
        if self.question == "still_true":
            allowed = self.fallback in ("revive", "archive")
        elif self.question == "paid":
            allowed = self.fallback == "history"
        else:
            allowed = self.fallback == "history" or (self.fallback == "archive" and self.noise)
        if not allowed:
            raise ValueError(f"a {self.question} question cannot fall back to {self.fallback}")

    def _check_attempt(self) -> None:
        if self.status is QuestionStatus.QUEUED:
            if any(getattr(self, name) is not None for name in _ATTEMPT_FIELDS):
                raise ValueError("a queued question has no attempt: no session, append, message, ask or reply")
        elif self.status is QuestionStatus.PENDING:
            if self.session_id is None:
                raise ValueError("a pending question has a session_id")
            if self.asked_at is not None:
                raise ValueError("a pending question has not been asked yet")
            if self.message_ref is not None and self.appended_at is None:
                raise ValueError("a posted question was appended first")
        elif self.status in SENT_STATUSES:
            if any(getattr(self, name) is None for name in _SENT_FIELDS):
                raise ValueError("an asked or unanswered question has asked_at, session_id, appended_at and a message_ref")

    def _check_reply(self) -> None:
        if (self.reply_text is None) != (self.reply_at is None):
            raise ValueError("reply_text and reply_at go together")
        if self.reply_text is not None and self.asked_at is None:
            raise ValueError("a reply needs a question that was asked")
        if self.offered_at is not None and self.reply_text is None:
            raise ValueError("offered_at needs a reply")

    def _check_closing(self) -> None:
        if self.closed_at is None or self.closed_by is None:
            raise ValueError("a closed question has closed_at and closed_by")
        if self.closed_by is ClosedBy.OWNER:
            if self.answer is None or self.answer_source not in ANSWER_COUNTERS:
                raise ValueError("an owner close has an answer and a source of digit, button or cabinet")
            if self.answer_source != "cabinet" and self.message_ref is None:
                raise ValueError("a digit or button answer needs the question's message_ref")
            return
        if self.answer is not None:
            raise ValueError("only an owner close has an answer")
        if self.closed_by is ClosedBy.REVIEW:
            if self.answer_source == "review":
                if self.reply_text is None:
                    raise ValueError("a review close on the owner's words needs a reply_text")
            elif self.answer_source is not None:
                raise ValueError("a review close has the source review or none")
            return
        if self.answer_source is not None:
            raise ValueError("a timeout or an elsewhere close has no answer source")
        if self.closed_by is ClosedBy.TIMEOUT and self.asked_at is None:
            raise ValueError("only a question that was asked times out")

    # ------------------------------------------------------------------------------------------------ reads

    @property
    def is_open(self) -> bool:
        """Its fact is in the review's hands: queued, being sent, sent, or sent and unanswered."""
        return self.status in OPEN_STATUSES

    def asked_of(self, account_id: str, user_id: str) -> bool:
        """The question belongs to this user in this account: the only one who may answer it."""
        return self.account_id == account_id and self.user_id == user_id

    @property
    def in_chat(self) -> bool:
        """It is sent to the owner's chat; a digest question is answered only in the Cabinet and never sent."""
        return self.delivery == "chat"

    @property
    def delivered(self) -> bool:
        """The owner was shown the question: it has a message ref (the post succeeded), whatever else was recorded."""
        return self.message_ref is not None

    @property
    def answerable(self) -> bool:
        """A reply or a click on its message counts: open and delivered (asked, unanswered, or pending with a ref)."""
        return self.is_open and self.delivered

    @property
    def retraction_due(self) -> bool:
        """The not-delivered frame must follow: the question's frame was appended but the post never succeeded. False
        again once `retracted`, so a retry that finds the question still pending appends no second retraction."""
        return self.status is QuestionStatus.PENDING and not self.delivered and self.appended_at is not None

    @property
    def reply_not_shown(self) -> bool:
        """An answer in words was recorded and the review has not read it yet."""
        return self.is_open and bool(self.reply_text) and self.offered_at is None

    def is_timed_out(self, now: datetime) -> bool:
        return self.status in SENT_STATUSES and self.asked_at is not None and self.asked_at + QUESTION_TIMEOUT <= now

    # ------------------------------------------------------------------------------------------------ moves

    def claimed(self, session_id: str) -> Self:
        """queued -> pending, for the session the question will be sent in."""
        self._require(self.status is QuestionStatus.QUEUED, "claimed")
        return self._moved(status=QuestionStatus.PENDING, session_id=session_id)

    def appended(self, at: datetime) -> Self:
        """The question's frame and text were appended to the session history (once)."""
        self._require(self.status is QuestionStatus.PENDING and self.appended_at is None, "appended")
        return self._moved(appended_at=at)

    def posted(self, ref: MessageRef) -> Self:
        """The post succeeded and landed at `ref` (once, and only after the frame was appended)."""
        self._require(self.status is QuestionStatus.PENDING and self.appended_at is not None and not self.delivered, "posted")
        return self._moved(message_ref=ref)

    def retracted(self) -> Self:
        """The not-delivered frame now follows the question's frame in history: the frame no longer stands, so
        `appended_at` is cleared (once; only while `retraction_due`). The question stays pending until it is requeued,
        and it can no longer be posted (`posted` needs an appended frame)."""
        self._require(self.retraction_due, "retracted")
        return self._moved(appended_at=None)

    def asked(self, at: datetime) -> Self:
        """pending, posted -> asked."""
        self._require(self.status is QuestionStatus.PENDING and self.delivered, "asked")
        return self._moved(status=QuestionStatus.ASKED, asked_at=at)

    def completed(self) -> Self:
        """A delivered `pending` question whose move to asked was lost: asked, at the latest time known to precede the
        post (`appended_at`). The one rule for housekeeping, an intake reply and a click that find it so."""
        self._require(self.status is QuestionStatus.PENDING and self.delivered, "completed")
        return self._moved(status=QuestionStatus.ASKED, asked_at=self.appended_at)

    def requeued(self) -> Self:
        """A pending question that was never posted goes back to the queue, its place (`queue_key`) kept and the
        attempt forgotten. Read `retraction_due` before: the not-delivered frame is the caller's."""
        self._require(self.status is QuestionStatus.PENDING and not self.delivered, "requeued")
        return self._moved(status=QuestionStatus.QUEUED, **{name: None for name in _ATTEMPT_FIELDS})

    def unanswered(self) -> Self:
        """asked -> unanswered: an earlier day's question, still open and still timing out."""
        self._require(self.status is QuestionStatus.ASKED, "unanswered")
        return self._moved(status=QuestionStatus.UNANSWERED)

    def with_reply(self, text: str, at: datetime) -> Self:
        """The owner's words, added to those already recorded (joined by a new line) up to `MAX_REPLY_CHARS`; what does
        not fit is dropped, and words that add nothing leave the question as it is."""
        self._require(self.status in SENT_STATUSES and self.offered_at is None, "with_reply")
        words = text.strip()
        if not words:
            raise QuestionTransitionError("a reply with no words is not recorded")
        joined = words if self.reply_text is None else f"{self.reply_text}\n{words}"
        kept = joined[:MAX_REPLY_CHARS]
        if self.reply_text is not None and not kept[len(self.reply_text):].strip():
            return self
        return self._moved(reply_text=kept, reply_at=at)

    def offered(self, at: datetime) -> Self:
        """The review has been shown the recorded reply (once)."""
        self._require(self.status in SENT_STATUSES and self.reply_not_shown, "offered")
        return self._moved(offered_at=at)

    def closed(
        self,
        by: ClosedBy,
        at: datetime,
        answer: Optional[AnswerKind] = None,
        source: Optional[AnswerSource] = None,
    ) -> Self:
        """open -> closed, kept `QUESTION_RETENTION` from `at`. An owner close carries the answer and its source; a review
        close on the owner's words has source `review`; the others carry neither."""
        self._require(self.is_open, "closed")
        return self._moved(
            status=QuestionStatus.CLOSED, closed_by=by, closed_at=at, answer=answer, answer_source=source,
        )

    def fallback_failed(self) -> Self:
        """Its history fallback could not be written this time: one more attempt counted, the question still open."""
        self._require(self.is_open, "fallback_failed")
        return self._moved(fallback_attempts=self.fallback_attempts + 1)

    # ------------------------------------------------------------------------------------------------ plumbing

    def model_copy(self, *, update: Optional[Mapping[str, Any]] = None, deep: bool = False) -> Self:
        """A copy that is validated again, so no caller can make an illegal question."""
        return type(self).model_validate({**super().model_copy(update=update, deep=deep).model_dump()})

    def _require(self, allowed: bool, move: str) -> None:
        if not allowed:
            raise QuestionTransitionError(f"a {self.status.value} question cannot be {move}")

    def _moved(self, **changes: Any) -> Self:
        try:
            return type(self).model_validate({**self.model_dump(), **changes})
        except ValidationError as exc:
            raise QuestionTransitionError(str(exc)) from exc
