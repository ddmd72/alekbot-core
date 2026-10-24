"""
FirestoreFactReviewStore — FactReviewStore on Firestore (Facts S4 plan §2.2, §3.13).

`{prefix}fact_review_questions/{question_id}`: a `FactReviewQuestion` as its own `model_dump`, with the message ref held as
`message_platform`, `message_channel` and `message_id` (null until posted), statuses and closers as their string values,
`fallback_date` as an ISO date string and every datetime as an aware UTC timestamp. Its TTL field is `purge_after`, set
only on a closed question; no field is named `expires_at`.

Every query is served by single-field indexes (an equality next to an `in`, or four equalities; the parity scenario
`equality_and_in_without_composite`); queue order is the domain's, in code. A write reads the question in a transaction,
calls the question's own move and writes the result: this adapter decides no transition.
"""
from datetime import date, datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, TypeVar

from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from pydantic import ValidationError

from ..config.environment import EnvironmentConfig
from ..domain.exceptions import QuestionTransitionError
from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_question import AnswerSource, ClosedBy, FactReviewQuestion, QuestionStatus
from ..domain.messaging import MessageRef
from ..ports.fact_review_store import FactReviewStore
from ..utils.logger import logger

T = TypeVar("T")

OPEN_STATUSES = [s.value for s in (QuestionStatus.QUEUED, QuestionStatus.PENDING, QuestionStatus.ASKED, QuestionStatus.UNANSWERED)]
_REF_FIELDS = ("message_platform", "message_channel", "message_id")


def _stored(value: Any) -> Any:
    """A model value as Firestore stores it: enums by value, a calendar date as its ISO string (the client refuses a
    `date`), maps and lists element by element; a datetime stays a timestamp."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _stored(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stored(v) for v in value]
    return value


def question_document(question: FactReviewQuestion) -> Dict[str, Any]:
    """The stored form of a question: its `model_dump`, the ref split into the three message fields."""
    data = question.model_dump()
    ref = data.pop("message_ref")
    data["message_platform"] = None if ref is None else ref["platform"]
    data["message_channel"] = None if ref is None else ref["channel_id"]
    data["message_id"] = None if ref is None else ref["message_id"]
    return {k: _stored(v) for k, v in data.items()}


def question_from(data: Dict[str, Any]) -> FactReviewQuestion:
    """A stored question validated by the model (raises `ValidationError` for one it no longer accepts)."""
    fields = dict(data)
    platform, channel, message = (fields.pop(name, None) for name in _REF_FIELDS)
    if platform is not None or channel is not None or message is not None:
        fields["message_ref"] = {"platform": platform, "channel_id": channel, "message_id": message}
    return FactReviewQuestion.model_validate(fields)


class FirestoreFactReviewStore(FactReviewStore):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._questions = db_client.collection(env_config.fact_review_questions_collection)

    # ------------------------------------------------------------------------------------------------ reads

    async def get_questions(self, ids: Sequence[str]) -> List[FactReviewQuestion]:
        wanted = list(dict.fromkeys(i for i in ids if i))
        if not wanted:
            return []
        found: Dict[str, FactReviewQuestion] = {}
        async for snap in self._db.get_all([self._questions.document(i) for i in wanted]):
            if snap.exists:
                question = self._readable(snap)
                if question is not None:
                    found[snap.id] = question
        return [found[i] for i in wanted if i in found]

    async def questions_for_account(self, account_id: str) -> List[FactReviewQuestion]:
        if not account_id:
            raise ValueError("account_id is required to read the account's review questions")
        return self._valid(await self._open_of_account(account_id).get())

    async def all_open(self) -> List[FactReviewQuestion]:
        return self._valid(await self._questions.where(filter=FieldFilter("status", "in", OPEN_STATUSES)).get())

    async def by_message(self, user_id: str, platform: str, channel_id: str, message_id: str) -> Optional[FactReviewQuestion]:
        if not (user_id and platform and channel_id and message_id):
            return None
        query = (
            self._questions.where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("message_platform", "==", platform))
            .where(filter=FieldFilter("message_channel", "==", channel_id))
            .where(filter=FieldFilter("message_id", "==", message_id))
        )
        found = self._valid(await query.get())
        if len(found) > 1:
            logger.error(f"❌ [FactReviewStore] {len(found)} questions share one message; the first by id is used")
        return min(found, key=lambda q: q.question_id) if found else None

    # ------------------------------------------------------------------------------------------------ question writes

    async def set_message_ref(self, question_id: str, ref: MessageRef) -> bool:
        return await self._move(question_id, lambda q: q.posted(ref))

    async def record_reply(self, question_id: str, text: str, at: datetime) -> bool:
        return await self._move(question_id, lambda q: q.with_reply(text, at))

    async def mark_offered(self, question_id: str, at: datetime) -> bool:
        return await self._move(question_id, lambda q: q.offered(at))

    async def close(
        self,
        question_id: str,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        at: datetime,
    ) -> bool:
        return await self._move(question_id, lambda q: q.closed(closed_by, at, answer, source))

    # ------------------------------------------------------------------------------------------------ plumbing

    def _open_of_account(self, account_id: str) -> Any:
        return (
            self._questions.where(filter=FieldFilter("account_id", "==", account_id))
            .where(filter=FieldFilter("status", "in", OPEN_STATUSES))
        )

    def _readable(self, snap: Any) -> Optional[FactReviewQuestion]:
        try:
            return question_from(snap.to_dict() or {})
        except ValidationError as exc:
            logger.error(f"❌ [FactReviewStore] question {snap.id[:12]} no longer validates and is skipped: "
                         f"{exc.error_count()} error(s)")
            return None

    def _valid(self, snaps: Iterable[Any]) -> List[FactReviewQuestion]:
        return [q for q in (self._readable(snap) for snap in snaps) if q is not None]

    def _the_question(self, snap: Any) -> FactReviewQuestion:
        """The one question a write is about: an unreadable one is logged and raised, never answered as refused."""
        try:
            return question_from(snap.to_dict() or {})
        except ValidationError:
            logger.error(f"❌ [FactReviewStore] question {snap.id[:12]} no longer validates; the write is not applied")
            raise

    async def _move(self, question_id: str, move: Callable[[FactReviewQuestion], FactReviewQuestion]) -> bool:
        """Read the question, apply `move`, write the result, in one transaction. False when the question is missing,
        the move is refused, or it returns the question unchanged."""
        if not question_id:
            return False
        ref = self._questions.document(question_id)

        async def body(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False
            before = self._the_question(snap)
            try:
                after = move(before)
            except QuestionTransitionError as exc:
                logger.info(f"[FactReviewStore] question {question_id[:12]} not moved: {exc}")
                return False
            if after is before:
                return False
            transaction.set(ref, question_document(after))
            return True

        return await self._in_transaction(body, question_id)

    async def _in_transaction(self, body: Callable[[Any], Awaitable[T]], question_id: str) -> T:
        """Run `body` in a fresh transaction; an aborted commit re-runs it (the client's retries), so every decision is
        taken again on what the retry reads. An error after the last attempt is logged and raised."""
        @firestore.async_transactional
        async def run(transaction: Any) -> T:
            return await body(transaction)

        try:
            return await run(self._db.transaction())
        except Exception as exc:
            logger.error(f"❌ [FactReviewStore] transaction on question {question_id[:12]} failed: {type(exc).__name__}: {exc}")
            raise
