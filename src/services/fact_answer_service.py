"""FactAnswerService — the owner's answers to memory-review questions, and the one way a question closes (Facts S4 plan
§3.16, §3.7).

`apply` is the one path of an owner answer (a digit, a button, a Cabinet click): the domain decides the write
(`answer_updates`) and its trail reason; this service checks the question, follows the fact forward, writes through the
typed write path or in place, and closes. `close_question` is every close (the owner's, the review's, a timeout,
`resolved_elsewhere`): it clears the current slot with the close and releases the next question, and re-renders the
question's message.
"""
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, Mapping, Optional

from ..domain.choice_outcome import ChoiceOutcome
from ..domain.exceptions import FactExpiryError, FactResolutionError, FactWriteConflict
from ..domain.fact_answer_result import AnswerResult
from ..domain.fact_lifecycle import walked_to_unseen_text
from ..domain.fact_lifecycle_day import CounterField
from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_answers import answer_updates
from ..domain.fact_review_language import LABEL_KEYS, fact_question_message
from ..domain.fact_review_question import AnswerSource, ClosedBy, FactReviewQuestion
from ..domain.fact_update_outcome import UpdateRefusal
from ..domain.fact_write_context import FactWriteContext
from ..domain.pass_actor import PassActor
from ..domain.ui_messages import UIMessage
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
from ..ports.fact_update_port import FactUpdatePort
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.repository import FactRepository
from ..ports.session_store import SessionStore
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger

if TYPE_CHECKING:
    from .fact_question_texts import FactQuestionTexts
    from .owner_timezone_resolver import OwnerTimezoneResolver

# The worker task that sends the next question once the current one is closed (its dispatch branch: T10a).
FACT_REVIEW_TASK = "fact_review"

# Who counts an owner answer, by where it came from.
_ANSWERED: Mapping[str, CounterField] = {
    "digit": CounterField.ANSWERED_BY_DIGIT,
    "button": CounterField.ANSWERED_BY_BUTTON,
    "cabinet": CounterField.ANSWERED_IN_CABINET,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactAnswerService:
    """`apply(account_id, user_id, question_id, kind, source, at)` and `close_question(question, closed_by, answer,
    source, note)`. Ports only; the texts and the owner's zone come from their one resolver each. `session_store` is
    for the reply's history pair (T8f)."""

    def __init__(
        self,
        fact_updates: FactUpdatePort,
        repo: FactRepository,
        review_store: FactReviewStore,
        counters: FactLifecycleCountersPort,
        task_queue: TaskQueue,
        session_store: SessionStore,
        texts: "FactQuestionTexts",
        interactive: InteractiveMessagePort,
        owner_zones: "OwnerTimezoneResolver",
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._fact_updates = fact_updates
        self._repo = repo
        self._review_store = review_store
        self._counters = counters
        self._task_queue = task_queue
        self._session_store = session_store
        self._texts = texts
        self._interactive = interactive
        self._owner_zones = owner_zones
        self._clock = clock

    async def apply(
        self, account_id: str, user_id: str, question_id: str, kind: AnswerKind, source: AnswerSource, at: datetime,
    ) -> AnswerResult:
        """Apply the owner's answer `kind` to the question's fact and close the question.

        One `now` from the clock serves the whole call: the domain's horizon, the trail event, the evidence moment and
        the close. `at` (when the owner answered, as the caller saw it) is not read: the clock's moment is the answer's.

        Results: `not_found` (no such question of this account and user) and `not_open` (closed already) write
        nothing. The fact is followed forward with the owner's write context; a fact gone, a chain that ends elsewhere,
        or a newer document whose text the owner never saw (`walked_to_unseen_text`) is `changed`, nothing written.
        Then the domain's `answer_updates` is written: an update through the typed write path, the in-place fallback
        fields (`dont_know`) under the repository's precondition. A conflict is `conflict`; any other refusal of the
        write, or a `FactExpiryError`, is `not_applied` (WARNING, `answers_not_applied`). Only an applied answer closes
        the question (`close_question`, the owner's close), refreshes the biographical cache once and is counted by its
        source. Raises ValueError for a source that is not the owner's; store errors raise."""
        counter = _ANSWERED.get(source)
        if counter is None:
            raise ValueError(f"{source!r} is not where an owner answer comes from")
        now = self._clock()
        question = next(iter(await self._review_store.get_questions([question_id])), None)
        if question is None or question.account_id != account_id or question.user_id != user_id:
            logger.info(f"[FactAnswer] question {question_id} not found for account {account_id[:8]}")
            return AnswerResult.NOT_FOUND
        if not question.is_open:
            logger.info(f"[FactAnswer] question {question_id} is {question.status.value}; answer not applied")
            return AnswerResult.NOT_OPEN
        zone = (await self._owner_zones.resolve(account_id)).zone
        context = FactWriteContext.for_pass(PassActor.OWNER, zone, None)
        try:
            stored = await self._repo.resolve_current(question.fact_id, account_id, context.accepts)
        except FactResolutionError as exc:
            logger.info(f"[FactAnswer] question {question_id}: fact {question.fact_id[:8]} cannot be followed: {exc}")
            return AnswerResult.CHANGED
        if stored is None or walked_to_unseen_text(question.quoted_text, stored.fact, question.fact_id):
            logger.info(f"[FactAnswer] question {question_id}: fact {question.fact_id[:8]} changed since it was asked")
            return AnswerResult.CHANGED
        try:
            change = answer_updates(kind, stored.fact, question, now, zone, source)
            if change.fields is not None:
                await self._repo.update_fact_fields(stored, change.fields)
            else:
                if change.update is None:
                    raise RuntimeError("an AnswerChange without fields carries its update")
                outcome = await self._fact_updates.apply_fact_update(
                    stored.fact.id, change.update, account_id, context=context,
                    evidence_at=change.evidence_at, reason=change.reason, history=change.history,
                )
                if outcome.refusal is UpdateRefusal.CONFLICT:
                    logger.warning(f"[FactAnswer] question {question_id}: the write conflicted; nothing written")
                    return AnswerResult.CONFLICT
                if not outcome.written:
                    return await self._not_applied(question, now, f"{outcome.refusal.value if outcome.refusal else None}: {outcome.refusal_text}")
        except FactWriteConflict:
            logger.warning(f"[FactAnswer] question {question_id}: the in-place write conflicted; nothing written")
            return AnswerResult.CONFLICT
        except FactExpiryError as exc:
            return await self._not_applied(question, now, f"expiry: {exc}")
        await self._close(question, ClosedBy.OWNER, kind, source, None, now)
        await self._repo.refresh_biographical_context_cache(owner_id=account_id)
        await self._count(account_id, now, {counter: 1})
        logger.info(f"[FactAnswer] question {question_id} answered {kind.value} ({source}); applied")
        return AnswerResult.APPLIED

    async def close_question(
        self,
        question: FactReviewQuestion,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind] = None,
        source: Optional[AnswerSource] = None,
        note: Optional[UIMessage] = None,
    ) -> bool:
        """Close `question` (`closed(closed_by, now, answer, source)`) and, in the same transaction, clear the current
        slot when it names this question (`close_and_release`). When it was the current one, the next question is
        released: one `fact_review` task `{user_id, account_id, mode: "next"}`, named after the closed question, so a
        retried close never enqueues twice. A question with a message ref has its message re-rendered without controls
        (`show_choice`): the chosen option for an answer, else `note` (default `MEMORY_QUESTION_CLOSED`), all texts from
        one `labels_for`; best effort, a failure is a WARNING and the close stands.

        Returns False, with nothing released or re-rendered, when the store refuses the close (no such question, closed
        already, or the answer and source do not fit the closer). Raises ValueError for a `note` that is not one of the
        review's texts."""
        if note is not None and note.value not in LABEL_KEYS:
            raise ValueError(f"{note.value} is not a memory review text")
        return await self._close(question, closed_by, answer, source, note, self._clock())

    # ------------------------------------------------------------------------------------------------ internals

    async def _close(
        self,
        question: FactReviewQuestion,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        note: Optional[UIMessage],
        now: datetime,
    ) -> bool:
        state = await self._review_store.get_state(question.account_id)
        was_current = state is not None and state.current_question_id == question.question_id
        closed = await self._review_store.close_and_release(question.question_id, closed_by, answer, source, now)
        if not closed:
            logger.warning(
                f"[FactAnswer] question {question.question_id} not closed by {closed_by.value}: the store refused it"
            )
            return False
        if was_current:
            await self._release_next(question)
        await self._show_closed(question, answer, note)
        return True

    async def _release_next(self, question: FactReviewQuestion) -> None:
        try:
            await self._task_queue.enqueue_worker_task(
                FACT_REVIEW_TASK,
                {"user_id": question.user_id, "account_id": question.account_id, "mode": "next"},
                dedup_key=f"fact-review-next:{question.question_id}",
            )
        except Exception as exc:
            # The close stands; the next question then goes out with the next daily run.
            logger.error(
                f"[FactAnswer] next question after {question.question_id} not released: {type(exc).__name__}: {exc}"
            )

    async def _show_closed(
        self, question: FactReviewQuestion, answer: Optional[AnswerKind], note: Optional[UIMessage],
    ) -> None:
        if question.message_ref is None:
            return
        try:
            labels = await self._texts.labels_for(question.user_id, question.account_id)
            message = fact_question_message(question, labels)
            if answer is not None:
                outcome = ChoiceOutcome(message=message, chosen_option_id=answer.value)
            else:
                outcome = ChoiceOutcome(message=message, note=labels[(note or UIMessage.MEMORY_QUESTION_CLOSED).value])
            await self._interactive.show_choice(question.message_ref, outcome)
        except Exception as exc:
            logger.warning(
                f"[FactAnswer] message of question {question.question_id} not re-rendered on "
                f"{question.message_ref.platform}: {type(exc).__name__}: {exc}"
            )

    async def _not_applied(self, question: FactReviewQuestion, now: datetime, why: str) -> AnswerResult:
        logger.warning(f"[FactAnswer] question {question.question_id}: the answer's write was refused ({why})")
        await self._count(question.account_id, now, {CounterField.ANSWERS_NOT_APPLIED: 1})
        return AnswerResult.NOT_APPLIED

    async def _count(self, account_id: str, now: datetime, counts: Dict[CounterField, int]) -> None:
        try:
            await self._counters.add(account_id, now.astimezone(timezone.utc).date(), counts)
        except Exception as exc:
            logger.warning(
                f"[FactAnswer] {', '.join(c.value for c in counts)} not counted for account {account_id[:8]}: "
                f"{type(exc).__name__}"
            )
