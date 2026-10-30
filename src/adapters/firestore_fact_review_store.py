"""
FirestoreFactReviewStore — FactReviewStore on Firestore (Facts S4 plan §2.2, §3.13).

`{prefix}fact_review_questions/{question_id}`: a `FactReviewQuestion` as its own `model_dump`, with the message ref held
as `message_platform`, `message_channel` and `message_id` (null until posted), statuses and closers as their string
values, `fallback_date` as an ISO date string and every datetime as an aware UTC timestamp.

`{prefix}fact_review_messages/{user_id}:digest:{iso_week}`: the weekly digest's claim (`user_id`, `kind` = `digest`,
`claimed_at`), created once; a release deletes it. `iso_week` is the domain's `iso_week_of(day)`.

`{prefix}fact_review_messages/{account_id}:quote:{language}:{source_digest}`: a quote translation (`account_id`, `kind` =
`quote`, `language`, `source_digest`, `text`, `written_at`, `purge_after` = `written_at + QUOTE_TRANSLATION_RETENTION`, the
collection's TTL policy, active since 2026-10-26); `purge_overdue` counts them with the digest claims.

TTL: the collections' Firestore TTL policies are on the field `purge_after`, which exists only in storage. It is
written at serialisation: on a closed question as `closed_at + QUESTION_RETENTION` (an open question has none), and on a
digest claim as `claimed_at + QUESTION_RETENTION`. A read ignores it. `purge_overdue` counts the documents whose
`purge_after` has passed, in both collections, to watch the TTL's lag. No field is named `expires_at`.

`{prefix}fact_review_state/{account_id}`: a `FactReviewState` as its `model_dump` (its read-only mappings as maps, its
calendar days as ISO date strings). This store writes only the fields a change touches, each replaced whole (a map
too: every state write is `set(..., merge=[fields])`, parity scenario `set_merge_fields_replaces_a_map_whole`), so
the other writers' fields stay as they are.

Every query is served by single-field indexes (an equality next to an `in`, or four equalities; the parity scenario
`equality_and_in_without_composite`; a range on `purge_after`); queue order is the domain's, in code. A write reads in a
transaction, calls the question's own move (and the domain's queue rules for a claim) and writes the result: this
adapter decides no transition.
"""
from datetime import date, datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeVar

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from pydantic import ValidationError

from ..config.environment import EnvironmentConfig
from ..domain.exceptions import QuestionTransitionError
from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_question_close import QuestionClose
from ..domain.fact_review_reply_record import ReplyRecord
from ..domain.fact_review_quote_translation import QUOTE_TRANSLATION_RETENTION, QuoteTranslation
from ..domain.fact_review_question import (
    OPEN_STATUSES, QUESTION_RETENTION, AnswerSource, ClosedBy, FactReviewQuestion, QuestionStatus,
)
from ..domain.fact_review_rules import current_question, iso_week_of, may_send, next_in_queue
from ..domain.fact_review_state import FactReviewState
from ..domain.messaging import MessageRef
from ..ports.fact_review_store import FactReviewStore
from ..utils.logger import logger

T = TypeVar("T")

_OPEN_VALUES = [status.value for status in QuestionStatus if status in OPEN_STATUSES]
_REF_FIELDS = ("message_platform", "message_channel", "message_id")
# The field the collections' TTL policies are on; storage only, never a model field.
_TTL_FIELD = "purge_after"
# What a state change of this store is allowed to touch; anything else in the state document belongs to other writers.
_STATE_FIELDS = frozenset({
    "current_question_id", "sent", "sent_on", "delivery_failed_on",
    "enqueued_at", "started_at", "last_review_at", "last_failed_at", "unsuccessful_since", "offers",
    "last_digest_at", "undigested_since", "oldest_alert_at", "queue_alert_at", "message_language", "labels",
    "label_digests",
})

# A state change: from the state read in the transaction to the fields to change (empty: leave the state alone).
StateChange = Callable[[FactReviewState], Dict[str, Any]]


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
    """The stored form of a question: its `model_dump`, the ref split into the three message fields, and the TTL field
    on a closed one."""
    data = question.model_dump()
    ref = data.pop("message_ref")
    data["message_platform"] = None if ref is None else ref["platform"]
    data["message_channel"] = None if ref is None else ref["channel_id"]
    data["message_id"] = None if ref is None else ref["message_id"]
    if question.closed_at is not None:
        data[_TTL_FIELD] = question.closed_at + QUESTION_RETENTION
    return {k: _stored(v) for k, v in data.items()}


def question_from(data: Dict[str, Any]) -> FactReviewQuestion:
    """A stored question validated by the model (raises `ValidationError` for one it no longer accepts); the TTL
    field is not read."""
    fields = dict(data)
    fields.pop(_TTL_FIELD, None)
    platform, channel, message = (fields.pop(name, None) for name in _REF_FIELDS)
    if platform is not None or channel is not None or message is not None:
        fields["message_ref"] = {"platform": platform, "channel_id": channel, "message_id": message}
    return FactReviewQuestion.model_validate(fields)


def state_document(state: FactReviewState) -> Dict[str, Any]:
    """The stored form of a review state: its `model_dump`, calendar days as ISO strings."""
    return {k: _stored(v) for k, v in state.model_dump().items()}


def state_from(data: Dict[str, Any]) -> FactReviewState:
    """A stored review state validated by the model (raises `ValidationError` for one it no longer accepts)."""
    return FactReviewState.model_validate(data)


class FirestoreFactReviewStore(FactReviewStore):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._questions = db_client.collection(env_config.fact_review_questions_collection)
        self._states = db_client.collection(env_config.fact_review_state_collection)
        self._messages = db_client.collection(env_config.fact_review_messages_collection)

    # ------------------------------------------------------------------------------------------------ reads

    async def get_questions(self, ids: Sequence[str]) -> List[FactReviewQuestion]:
        """One `get_all` over the wanted documents."""
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
        """Served by single-field indexes (an equality and an `in`)."""
        if not account_id:
            raise ValueError("account_id is required to read the account's review questions")
        return self._valid(await self._open_of_account(account_id).get())

    async def timeout_histories(self, account_id: str) -> List[FactReviewQuestion]:
        """Three equalities (`account_id`, `closed_by`, `fallback`): merged single-field indexes, no composite index."""
        if not account_id:
            raise ValueError("account_id is required to read the account's timeout histories")
        query = (
            self._questions.where(filter=FieldFilter("account_id", "==", account_id))
            .where(filter=FieldFilter("closed_by", "==", ClosedBy.TIMEOUT.value))
            .where(filter=FieldFilter("fallback", "==", "history"))
        )
        return self._valid(await query.get())

    async def all_open(self) -> List[FactReviewQuestion]:
        return self._valid(await self._questions.where(filter=FieldFilter("status", "in", _OPEN_VALUES)).get())

    async def by_message(self, user_id: str, ref: MessageRef) -> Optional[FactReviewQuestion]:
        """Equalities on the stored fields `user_id`, `message_platform`, `message_channel` and `message_id`."""
        if not (user_id and ref.platform and ref.channel_id and ref.message_id):
            return None
        query = (
            self._questions.where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("message_platform", "==", ref.platform))
            .where(filter=FieldFilter("message_channel", "==", ref.channel_id))
            .where(filter=FieldFilter("message_id", "==", ref.message_id))
        )
        found = self._valid(await query.get())
        if len(found) > 1:
            logger.error(f"❌ [FactReviewStore] {len(found)} questions share one message; none is answered by it")
            return None
        return found[0] if found else None

    async def get_state(self, account_id: str) -> Optional[FactReviewState]:
        if not account_id:
            raise ValueError("account_id is required to read the review state")
        snap = await self._states.document(account_id).get()
        return self._the_state(snap) if snap.exists else None

    # ------------------------------------------------------------------------------------------------ question writes

    async def set_message_ref(self, question_id: str, ref: MessageRef) -> bool:
        return await self._move(question_id, lambda q: q.posted(ref))

    async def record_reply(self, question_id: str, text: str, at: datetime) -> ReplyRecord:
        moved, state_written = await self._move_with_state(
            question_id, lambda q: q.with_reply(text, at), lambda state: state.released(question_id),
        )
        if not moved:
            return ReplyRecord.REFUSED
        return ReplyRecord.RELEASED if state_written else ReplyRecord.RECORDED

    async def mark_offered(self, question_id: str, at: datetime) -> bool:
        return await self._move(question_id, lambda q: q.offered(at))

    async def mark_fallback_failed(self, question_id: str) -> bool:
        return await self._move(question_id, lambda q: q.fallback_failed())

    async def close(
        self,
        question_id: str,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        at: datetime,
    ) -> bool:
        return await self._move(question_id, lambda q: q.closed(closed_by, at, answer, source))

    # ------------------------------------------------------------------------------------ queue and current question

    async def create_queued(self, question: FactReviewQuestion) -> bool:
        if question.status is not QuestionStatus.QUEUED:
            raise ValueError(f"only a queued question is created, not a {question.status.value} one")
        try:
            await self._questions.document(question.question_id).create(question_document(question))
        except AlreadyExists:
            logger.info(f"[FactReviewStore] question {question.question_id[:12]} exists; not created again")
            return False
        return True

    async def claim_next(self, account_id: str, today: date, session_id: str) -> Optional[FactReviewQuestion]:
        if not account_id:
            raise ValueError("account_id is required to claim a review question")
        if not session_id:
            raise ValueError("session_id is required to claim a review question")
        if isinstance(today, datetime):
            raise ValueError("today must be a date, not a datetime")
        state_ref = self._states.document(account_id)

        async def body(transaction: Any) -> Optional[FactReviewQuestion]:
            snap = await state_ref.get(transaction=transaction)
            state = self._the_state(snap) if snap.exists else FactReviewState(account_id=account_id)
            snaps = await self._open_of_account(account_id).get(transaction=transaction)
            # The pointed-at question decides whether one is out: unreadable, it is raised, never skipped (a skipped one
            # would read as "nothing out" and a second question would go). Other unreadable ones are skipped.
            read = [self._the_question(s) if s.id == state.current_question_id else self._readable(s) for s in snaps]
            questions = [q for q in read if q is not None]
            if not may_send(state, today, current_question(state, questions)):
                return None
            chosen = next_in_queue(questions)
            if chosen is None:
                return None
            try:
                claimed = chosen.claimed(session_id)
            except QuestionTransitionError as exc:
                logger.info(f"[FactReviewStore] question {chosen.question_id[:12]} not claimed: {exc}")
                return None
            state_write = self._state_write(state, state.claimed(claimed.question_id, today))
            transaction.set(self._questions.document(claimed.question_id), question_document(claimed))
            transaction.set(state_ref, state_write, merge=list(state_write))
            return claimed

        return await self._in_transaction(body, f"claim for account {account_id[:8]}")

    async def mark_appended(self, question_id: str, at: datetime) -> bool:
        return await self._move(question_id, lambda q: q.appended(at))

    async def mark_retracted(self, question_id: str) -> bool:
        return await self._move(question_id, lambda q: q.retracted())

    async def mark_asked(self, question_id: str, at: datetime) -> bool:
        return await self._move(question_id, lambda q: q.asked(at))

    async def complete_delivered(self, question_id: str) -> bool:
        return await self._move(question_id, lambda q: q.completed())

    async def requeue_front(self, question_id: str, failed_today: bool) -> bool:
        def change(state: FactReviewState) -> Dict[str, Any]:
            changes = state.released(question_id)
            if failed_today:
                unrecorded = state.unrecorded_delivery_failure(question_id)
                if unrecorded is not None:
                    logger.warning(f"⚠️ [FactReviewStore] failed delivery of {question_id[:12]}, {unrecorded}; "
                                   f"delivery_failed_on not set")
                changes.update(state.delivery_failed(question_id))
            return changes

        return await self._move(question_id, lambda q: q.requeued(), change)

    async def mark_unanswered(self, question_id: str) -> bool:
        return await self._move(question_id, lambda q: q.unanswered(), lambda state: state.released(question_id))

    async def close_and_release(
        self,
        question_id: str,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        at: datetime,
    ) -> QuestionClose:
        moved, state_written = await self._move_with_state(
            question_id, lambda q: q.closed(closed_by, at, answer, source), lambda state: state.released(question_id),
        )
        if not moved:
            return QuestionClose.REFUSED
        return QuestionClose.RELEASED if state_written else QuestionClose.CLOSED

    # ------------------------------------------------------------------------------------------------ state marks

    async def all_states(self) -> List[FactReviewState]:
        states: List[FactReviewState] = []
        async for snap in self._states.stream():
            try:
                states.append(state_from(snap.to_dict() or {}))
            except ValidationError as exc:
                logger.error(f"❌ [FactReviewStore] review state {snap.id[:8]} no longer validates and is skipped: "
                             f"{exc.error_count()} error(s)")
        return states

    async def mark_enqueued(self, account_id: str, at: datetime) -> bool:
        return await self._mark(
            account_id,
            lambda state: state.enqueued(at),
            create=True,
        )

    async def mark_started(self, account_id: str, at: datetime) -> bool:
        return await self._mark(account_id, lambda state: {"started_at": at})

    async def mark_succeeded(self, account_id: str, at: datetime) -> bool:
        return await self._mark(account_id, lambda state: state.succeeded(at))

    async def mark_failed(self, account_id: str, at: datetime) -> bool:
        return await self._mark(account_id, lambda state: state.failed(at))

    async def set_offers(self, account_id: str, offers: Mapping[str, int]) -> bool:
        return await self._mark(account_id, lambda state: {"offers": dict(offers)})

    async def mark_removed(self, account_id: str, fact_ids: Sequence[str]) -> bool:
        return await self._mark(account_id, lambda state: state.without_offers(fact_ids))

    async def mark_digest(self, account_id: str, at: datetime) -> bool:
        return await self._mark(account_id, lambda state: state.digested(at))

    async def mark_undigested(self, account_id: str, at: datetime) -> bool:
        return await self._mark(account_id, lambda state: state.undigested(at))

    async def mark_oldest_alert(self, account_id: str, at: Optional[datetime]) -> bool:
        return await self._mark(account_id, lambda state: {"oldest_alert_at": at})

    async def mark_queue_alert(self, account_id: str, at: Optional[datetime]) -> bool:
        return await self._mark(account_id, lambda state: {"queue_alert_at": at})

    async def set_language(self, account_id: str, lang: str, labels: Mapping[str, str], labels_digest: str) -> bool:
        return await self._mark(account_id, lambda state: state.with_language(lang, labels, labels_digest))

    # ------------------------------------------------------------------------------------------------ digest claim

    async def claim_digest(self, user_id: str, day: date, at: datetime) -> bool:
        ref = self._digest_ref(user_id, day)
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        document = {"user_id": user_id, "kind": "digest", "claimed_at": at, _TTL_FIELD: at + QUESTION_RETENTION}
        try:
            await ref.create(document)
        except AlreadyExists:
            return False
        return True

    async def release_digest(self, user_id: str, day: date) -> None:
        await self._digest_ref(user_id, day).delete()

    # ------------------------------------------------------------------------------------------------ quote translations

    async def set_quote_translations(self, account_id: str, translations: Sequence[QuoteTranslation], at: datetime) -> None:
        if not account_id:
            raise ValueError("account_id is required for a quote translation")
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        if not translations:
            return
        documents = [
            (self._quote_ref(account_id, t.language, t.source_digest), {
                "account_id": account_id, "kind": "quote", "language": t.language, "source_digest": t.source_digest,
                "text": t.text, "written_at": at, _TTL_FIELD: at + QUOTE_TRANSLATION_RETENTION,
            })
            for t in translations
        ]

        async def body(transaction: Any) -> None:
            for ref, document in documents:
                transaction.set(ref, document)

        await self._in_transaction(body, f"quote translations of account {account_id[:8]}")

    async def get_quote_translations(
        self, account_id: str, language: str, digests: Sequence[str],
    ) -> Dict[str, QuoteTranslation]:
        if not account_id:
            raise ValueError("account_id is required for a quote translation")
        wanted = list(dict.fromkeys(d for d in digests if d))
        if not wanted:
            return {}
        found: Dict[str, QuoteTranslation] = {}
        async for snap in self._db.get_all([self._quote_ref(account_id, language, d) for d in wanted]):
            if not snap.exists:
                continue
            data = snap.to_dict() or {}
            try:
                if data.get("kind") != "quote" or data.get("account_id") != account_id or data.get("language") != language:
                    raise ValueError("not this account's quote translation")
                record = QuoteTranslation(language=data["language"], source_digest=data["source_digest"], text=data["text"])
            except (ValueError, KeyError, TypeError):
                logger.error(f"❌ [FactReviewStore] quote translation {snap.id[-24:]} no longer validates and is skipped")
                continue
            found[record.source_digest] = record
        return found

    # ------------------------------------------------------------------------------------------------ expiry

    async def purge_overdue(self, before: datetime) -> int:
        """One count aggregation per collection, a range on the TTL field."""
        if before.tzinfo is None:
            raise ValueError("before must be timezone-aware")
        total = 0
        for collection in (self._questions, self._messages):
            result = await collection.where(filter=FieldFilter(_TTL_FIELD, "<", before)).count(alias="n").get()
            total += int(result[0][0].value)
        return total

    # ------------------------------------------------------------------------------------------------ plumbing

    def _open_of_account(self, account_id: str) -> Any:
        return (
            self._questions.where(filter=FieldFilter("account_id", "==", account_id))
            .where(filter=FieldFilter("status", "in", _OPEN_VALUES))
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

    def _the_state(self, snap: Any) -> FactReviewState:
        """The account's state: an unreadable one is logged and raised (a claim on it would overwrite what it holds)."""
        try:
            return state_from(snap.to_dict() or {})
        except ValidationError:
            logger.error(f"❌ [FactReviewStore] review state {snap.id[:8]} no longer validates; nothing is applied")
            raise

    @staticmethod
    def _state_write(state: FactReviewState, changes: Dict[str, Any]) -> Dict[str, Any]:
        """The merged write of `changes`: validated as the whole state they make, then only those fields (and the
        account id, for a state document that does not exist yet)."""
        unknown = set(changes) - _STATE_FIELDS
        if unknown:
            raise ValueError(f"the review store does not write state fields {sorted(unknown)}")
        changed = FactReviewState.model_validate({**state.model_dump(), **changes})
        document = state_document(changed)
        return {"account_id": changed.account_id, **{name: document[name] for name in changes}}

    def _digest_ref(self, user_id: str, day: date) -> Any:
        if not user_id:
            raise ValueError("user_id is required for the digest claim")
        return self._messages.document(f"{user_id}:digest:{iso_week_of(day)}")

    def _quote_ref(self, account_id: str, language: str, digest: str) -> Any:
        return self._messages.document(f"{account_id}:quote:{language}:{digest}")

    async def _mark(self, account_id: str, change: StateChange, create: bool = False) -> bool:
        """Read the account's state and write `change(state)`, each field replaced whole, in one transaction. A
        missing state document is created from an empty state with `create`, else left missing (False)."""
        if not account_id:
            raise ValueError("account_id is required to mark the review state")
        state_ref = self._states.document(account_id)

        async def body(transaction: Any) -> bool:
            snap = await state_ref.get(transaction=transaction)
            if not snap.exists and not create:
                return False
            state = self._the_state(snap) if snap.exists else FactReviewState(account_id=account_id)
            write = self._state_write(state, change(state))
            transaction.set(state_ref, write, merge=list(write))
            return True

        return await self._in_transaction(body, f"review state of account {account_id[:8]}")

    async def _move(
        self,
        question_id: str,
        move: Callable[[FactReviewQuestion], FactReviewQuestion],
        state_change: Optional[StateChange] = None,
    ) -> bool:
        """Read the question (and, with `state_change`, its account's state), apply `move`, write the result, in one
        transaction. False when the question is missing, the move is refused or returns the question unchanged; the
        state is then not written either. A missing state document is left missing."""
        moved, _ = await self._move_with_state(question_id, move, state_change)
        return moved

    async def _move_with_state(
        self,
        question_id: str,
        move: Callable[[FactReviewQuestion], FactReviewQuestion],
        state_change: Optional[StateChange] = None,
    ) -> Tuple[bool, bool]:
        """`_move`, also reporting whether the state was written in the same transaction: (moved, state written)."""
        if not question_id:
            return False, False
        ref = self._questions.document(question_id)

        async def body(transaction: Any) -> Tuple[bool, bool]:
            snap = await ref.get(transaction=transaction)
            if not snap.exists:
                return False, False
            before = self._the_question(snap)
            try:
                after = move(before)
            except QuestionTransitionError as exc:
                logger.info(f"[FactReviewStore] question {question_id[:12]} not moved: {exc}")
                return False, False
            if after is before:
                return False, False
            state_write: Optional[Dict[str, Any]] = None
            state_ref = self._states.document(before.account_id)
            if state_change is not None:
                state_snap = await state_ref.get(transaction=transaction)
                if state_snap.exists:
                    state = self._the_state(state_snap)
                    changes = state_change(state)
                    if changes:
                        state_write = self._state_write(state, changes)
            transaction.set(ref, question_document(after))
            if state_write is not None:
                transaction.set(state_ref, state_write, merge=list(state_write))
            return True, state_write is not None

        return await self._in_transaction(body, f"question {question_id[:12]}")

    async def _in_transaction(self, body: Callable[[Any], Awaitable[T]], what: str) -> T:
        """Run `body` in a fresh transaction; an aborted commit re-runs it (the client's retries), so every decision is
        taken again on what the retry reads. An error after the last attempt is logged and raised."""
        @firestore.async_transactional
        async def run(transaction: Any) -> T:
            return await body(transaction)

        try:
            return await run(self._db.transaction())
        except Exception as exc:
            logger.error(f"❌ [FactReviewStore] transaction on {what} failed: {type(exc).__name__}: {exc}")
            raise

