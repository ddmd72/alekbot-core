"""FactAnswerService — the owner's answers to memory-review questions, and the one way a question closes (Facts S4 plan
§3.16, §3.7).

`apply` is the one path of an owner answer (a digit, a button, a Cabinet click): the domain decides the write
(`answer_updates`) and its trail reason; this service checks the question, follows the fact forward, writes through the
typed write path or in place, and closes. `close_question` is every close (the owner's, the review's, a timeout,
`resolved_elsewhere`): it clears the current slot with the close and releases the next question, and re-renders the
question's message. `answer_reply` is the owner's reply at intake: it finds the question by the message replied to, takes
the digit through `apply` or records the words (releasing the next question when this one was current), and appends the reply with its acknowledgement to history. `handle` is the
owner's click on a button of the question's message (`ChoiceHandlerPort`): the same `apply` for an open question, the
existing answer for a closed one.
"""
from datetime import datetime, timezone
import asyncio
from typing import TYPE_CHECKING, AbstractSet, Callable, Dict, Iterable, List, Optional, Tuple

from ..domain.choice_outcome import ChoiceOutcome
from ..domain.entities import FactState
from ..domain.exceptions import FactExpiryError, FactResolutionError, FactWriteConflict
from ..domain.fact_answer_result import AnswerResult
from ..domain.fact_lifecycle import walked_to_unseen_text
from ..domain.fact_lifecycle_day import CounterField, counter_day
from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_answers import (
    DECIDING_ANSWERS, answer_closes_fact, answer_fits, answer_updates, digit_answer, option_answer, option_id,
)
from ..domain.fact_review_language import OPTION_KEYS, fact_question_message, is_review_text
from ..domain.fact_review_question import (
    ANSWER_COUNTERS, FACT_QUESTION_KIND, AnswerSource, ClosedBy, FactReviewQuestion, QuestionKind,
)
from ..domain.fact_review_frame import reply_part
from ..domain.fact_review_question_close import QuestionClose
from ..domain.fact_review_reply_record import ReplyRecord
from ..domain.fact_review_overview import QueueEntry, queue_entries, user_questions
from ..domain.fact_review_reply import (
    ClickPath, ClickShow, ReplyPath, acknowledgement, click_outcome, click_path, needs_completing, reply_path,
)
from ..domain.fact_review_task_keys import FACT_REVIEW_TASK, next_key, review_payload
from ..domain.fact_update_outcome import UpdateRefusal
from ..domain.fact_write_context import FactWriteContext, as_utc
from ..domain.interactive_choice import InteractiveChoice
from ..domain.llm import Message, MessagePart
from ..domain.messaging import MessageRef
from ..domain.pass_actor import PassActor
from ..domain.stored_fact import StoredFact
from ..domain.ui_messages import UIMessage
from ..ports.choice_handler_port import ChoiceHandlerPort
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

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FactAnswerService(ChoiceHandlerPort):
    """`apply(account_id, user_id, question_id, kind, source, at)` and `close_question(question, closed_by, answer,
    source, note)`, `answer_reply(user_id, account_id, platform, channel_id, message_id, text, at)` and `handle(choice,
    user_id, account_id)`. Ports only; the texts and the owner's zone come from their one resolver each. `session_store`
    is for the reply's history pair."""

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

        `at` is when the owner answered, and the one moment of the whole call: the domain's `now` (the evidence, the
        kind of a still-true decision, an ended fact's month, a fallback's horizon and event), the close's `closed_at`
        and the counters' day. The clock is not read.

        Results: `not_found` (no such question of this account and user) and `not_open` (closed already) write
        nothing. The fact is followed forward with the owner's write context; a fact gone, a chain that ends elsewhere,
        or a newer document whose text the owner never saw (`walked_to_unseen_text`) is `changed`, nothing written.
        Then the domain's `answer_updates` is written: an update through the typed write path, the in-place fallback
        fields (`dont_know`) under the repository's precondition. A conflict is `conflict`; any other refusal of the
        write, or a `FactExpiryError`, is `not_applied` (WARNING, `answers_not_applied`); so is an answer that is not an option of
        the question's kind (`answer_fits`: `happened` on a `still_true` question, `dont_know` on a digest question), refused
        before the fact is read. Only an applied answer closes
        the question (`close_question`, the owner's close), refreshes the biographical cache once and is counted by its
        source; an answer that sets the fact aside (`answer_closes_fact`) also marks the account undigested. Raises
        ValueError for a source that is not the owner's; store errors raise."""
        counter = ANSWER_COUNTERS.get(source)
        if counter is None:
            raise ValueError(f"{source!r} is not where an owner answer comes from")
        now = as_utc(at)
        question = await self._question(question_id)
        if question is None or not question.asked_of(account_id, user_id):
            logger.info(f"[FactAnswer] question {question_id} not found for account {account_id[:8]}")
            return AnswerResult.NOT_FOUND
        if not question.is_open:
            logger.info(f"[FactAnswer] question {question_id} is {question.status.value}; answer not applied")
            return AnswerResult.NOT_OPEN
        if not answer_fits(kind, question):
            return await self._not_applied(question, now, f"{kind.value} is not an option of a {question.question} question")
        zone = (await self._owner_zones.resolve(account_id)).zone
        context = FactWriteContext.for_pass(PassActor.OWNER, zone, None)
        stored = await self._asked_fact(question, context.accepts)
        if stored is None:
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
                    expected_id=stored.fact.id,
                )
                if outcome.refusal is UpdateRefusal.CONFLICT:
                    logger.warning(f"[FactAnswer] question {question_id}: the write conflicted; nothing written")
                    return AnswerResult.CONFLICT
                if outcome.refusal is not None:
                    return await self._not_applied(question, now, f"{outcome.refusal.value}: {outcome.refusal_text}")
        except FactWriteConflict:
            logger.warning(f"[FactAnswer] question {question_id}: the in-place write conflicted; nothing written")
            return AnswerResult.CONFLICT
        except FactExpiryError as exc:
            return await self._not_applied(question, now, f"expiry: {exc}")
        if answer_closes_fact(kind, question):
            await self._note_set_aside(account_id, now)
        await self._close(question, ClosedBy.OWNER, kind, source, None, now)
        await self._repo.refresh_biographical_context_cache(owner_id=account_id)
        await self._count(account_id, now, {counter: 1})
        logger.info(f"[FactAnswer] question {question_id} answered {kind.value} ({source}); applied")
        return AnswerResult.APPLIED

    async def answer_reply(
        self, user_id: str, account_id: str, platform: str, channel_id: str, message_id: str, text: str, at: datetime,
    ) -> Optional[str]:
        """The owner's reply to the message `(platform, channel_id, message_id)`, taken at intake: the text to post back,
        or None when the message is ordinary conversation.

        The question is the one posted at that message (`by_message`: the same message id in another channel, platform or
        user's question is not it). No such question, or no words in the reply, is None. A closed question answers
        `MEMORY_ALREADY_ANSWERED`. An open, delivered one (`asked`, `unanswered`, or `pending` with its ref, which
        `completed()` first moves to asked) takes the path `reply_path` names: a lone digit, read on the message the
        owner saw, is applied through `apply` with source `digit` (`MEMORY_ANSWER_NOTED`; the next question is released
        by the close, if this one was current; `MEMORY_FACT_CHANGED` when the fact changed or the write conflicted, the
        question staying open); anything else is recorded for the review (`record_reply`, `MEMORY_REPLY_NOTED`); when that
        question was the current one, recording clears the slot and the next question is released at once, as after a
        digit. The words are applied by nothing here (the review reads them on its next run).

        The acknowledgement and the reply are appended to the question's session as one pair, the user part the owner's
        text verbatim under `reply_part`, the model part the acknowledgement; the append raises when it is not stored.
        Nothing is posted here. The labels are read once, so a fallback is counted once.

        None, with the message left to ordinary conversation and nothing lost, also when the reply could not be taken: a
        digit whose write the rules refused (`not_applied`) or for a question that is not this account's (`not_found`),
        words the store would not record (already shown to the review, or nothing more fits). Any error in the path is logged at
        ERROR, counted (`reply_intake_failed`) and gives None; a pair that fails to append after an applied digit is such
        an error, the answer stays applied."""
        try:
            return await self._answer_reply(user_id, account_id, platform, channel_id, message_id, text, as_utc(at))
        except Exception as exc:
            logger.error(
                f"[FactAnswer] reply to {platform} message {message_id} not taken: {type(exc).__name__}: {exc}",
                exc_info=True,
            )
            await self._count(account_id, as_utc(at), {CounterField.REPLY_INTAKE_FAILED: 1})
            return None

    async def _answer_reply(
        self, user_id: str, account_id: str, platform: str, channel_id: str, message_id: str, text: str, now: datetime,
    ) -> Optional[str]:
        question = await self._review_store.by_message(user_id, MessageRef(platform, channel_id, message_id))
        if question is None or not text.strip():
            return None
        labels = await self._texts.labels_for(user_id, account_id)
        digit = digit_answer(fact_question_message(question, labels), text) if question.is_open else None
        path = reply_path(question, digit)
        if path is None:
            logger.warning(f"[FactAnswer] question {question.question_id} is open but was never delivered; reply left alone")
            return None
        if path is not ReplyPath.ALREADY_ANSWERED and needs_completing(question):
            await self._review_store.complete_delivered(question.question_id)
        result: Optional[AnswerResult] = None
        if path is ReplyPath.DIGIT:
            if digit is None:
                raise RuntimeError("a digit path carries its digit")
            result = await self.apply(account_id, user_id, question.question_id, digit, "digit", now)
        elif path is ReplyPath.WORDS:
            recorded = await self._review_store.record_reply(question.question_id, text, now)
            if recorded is ReplyRecord.REFUSED:
                logger.info(f"[FactAnswer] question {question.question_id}: the reply was not recorded; left to conversation")
                return None
            if recorded is ReplyRecord.RELEASED:
                await self._release_next(question)
            await self._count(account_id, now, {CounterField.REPLIES_IN_WORDS: 1})
        else:
            await self._count(account_id, now, {CounterField.ALREADY_ANSWERED: 1})
        note = acknowledgement(path, result)
        if note is None:
            logger.info(f"[FactAnswer] question {question.question_id}: digit not applied ({result.value if result else None}); left to conversation")
            return None
        reply = labels[note.value]
        await self._append_reply(question, reply_part(question.question_id, text), reply)
        logger.info(f"[FactAnswer] question {question.question_id}: reply taken ({path.value}) and acknowledged")
        return reply

    @property
    def kind(self) -> str:
        return FACT_QUESTION_KIND

    async def handle(self, choice: InteractiveChoice, user_id: str, account_id: str) -> Optional[ChoiceOutcome]:
        """The owner's click on a button of a question's message: the outcome to show on the message, or None when
        nothing is to be shown.

        The question is the one the binding names; it must be this user's and this account's, and its option an answer
        (else None, nothing written). An **open** question must be the one posted at the clicked message
        (`message_ref` equal; a question not delivered, or another message, is None); a delivered `pending` one is
        completed first (`completed()`, as at a reply), then the answer is applied through `apply` with source `button`,
        at the time of the click, and the outcome shows the chosen option. A `changed` or `conflict` apply shows
        `MEMORY_FACT_CHANGED` as the note with the options kept (`keep_options`): the question stays open, nothing was
        written, and the owner may click again. A `not_applied` one is None: the question stays open with its buttons
        untouched, and `apply` has logged why. A question that closes while the click is applied is treated as closed. A **closed** question shows its existing answer (its
        note when it has none) and writes nothing; one with no stored ref (closed during its own post) is shown on the
        clicked message, which heals it on its first click, while a stored ref that is not the clicked message gives
        None. Store errors raise."""
        question = await self._question(choice.binding.id)
        if question is None or not question.asked_of(account_id, user_id):
            logger.info(f"[FactAnswer] choice on question {choice.binding.id}: not this user's question; ignored")
            return None
        answer = option_answer(choice.option_id)
        if answer is None:
            logger.warning(f"[FactAnswer] choice on question {question.question_id}: {choice.option_id!r} is no answer")
            return None
        path = click_path(question, choice.message_ref)
        if path is ClickPath.IGNORED:
            logger.info(f"[FactAnswer] choice on question {question.question_id}: not the question's message; ignored")
            return None
        if path is ClickPath.OPEN:
            if needs_completing(question):
                await self._review_store.complete_delivered(question.question_id)
            result = await self.apply(account_id, user_id, question.question_id, answer, "button", choice.at)
            outcome = click_outcome(result)
            if outcome.show is ClickShow.CHOSEN:
                return await self._closed_outcome(question, answer, None)
            if outcome.show is ClickShow.KEPT and outcome.note is not None:
                return await self._kept_outcome(question, outcome.note)
            if outcome.show is not ClickShow.CLOSED:
                return None
            question = await self._question(question.question_id)
            if question is None:
                return None
            if click_path(question, choice.message_ref) is ClickPath.IGNORED:
                logger.info(f"[FactAnswer] choice on closed question {question.question_id}: not its message; ignored")
                return None
        return await self._closed_outcome(question, question.answer, None)

    async def review_queue(self, account_id: str, user_id: str) -> List[QueueEntry]:
        """The user's open questions in the order they are dealt with (`queue_entries`), each with its fact as an answer
        would find it: followed forward as the owner's answer follows it, so a fact rewritten since the question shows the
        question's quote (nothing the owner never saw), and a fact gone shows the quote too. Another account's or another
        user's questions are never listed."""
        mine = user_questions(await self._review_store.questions_for_account(account_id), account_id, user_id)
        state = await self._review_store.get_state(account_id)
        found = await asyncio.gather(*(self._asked_fact(q, PassActor.OWNER.accepts) for q in mine))
        facts = {q.fact_id: stored.fact for q, stored in zip(mine, found) if stored is not None}
        return queue_entries(state, mine, facts, account_id)

    async def answer_options(
        self, user_id: str, kinds: Iterable[QuestionKind],
    ) -> Dict[QuestionKind, List[Tuple[AnswerKind, str]]]:
        """The answers a click on the Cabinet can give to a question of each of `kinds`, each with its label in the user's
        UI language; the labels are read once."""
        labels = await self._texts.ui_labels(user_id)
        return {kind: [(a, labels[OPTION_KEYS[a]]) for a in DECIDING_ANSWERS[kind]] for kind in dict.fromkeys(kinds)}

    async def close_question(
        self,
        question: FactReviewQuestion,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind] = None,
        source: Optional[AnswerSource] = None,
        note: Optional[UIMessage] = None,
    ) -> bool:
        """Close `question` (`closed(closed_by, now, answer, source)`, `now` the clock's) and, in the same transaction,
        clear the current slot when it names this question (`close_and_release`). When that transaction cleared it, the
        next question is released: one `fact_review` task `{user_id, account_id, mode: "next"}`, named after the closed
        question, so a retried close never enqueues twice. A question with a message ref has its message re-rendered without controls
        (`show_choice`): the chosen option for an answer, else `note` (default `MEMORY_QUESTION_CLOSED`), all texts from
        one `labels_for`; best effort, a failure is a WARNING and the close stands.

        Returns False, with nothing released or re-rendered, when the store refuses the close (no such question, closed
        already, or the answer and source do not fit the closer). Raises ValueError for a `note` that is not one of the
        review's texts."""
        if note is not None and not is_review_text(note):
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
        closed = await self._review_store.close_and_release(question.question_id, closed_by, answer, source, now)
        if closed is QuestionClose.REFUSED:
            logger.warning(
                f"[FactAnswer] question {question.question_id} not closed by {closed_by.value}: the store refused it"
            )
            return False
        if closed is QuestionClose.RELEASED:
            await self._release_next(question)
        await self._show_closed(question, answer, note)
        return True

    async def _release_next(self, question: FactReviewQuestion) -> None:
        try:
            await self._task_queue.enqueue_worker_task(
                FACT_REVIEW_TASK,
                review_payload(question.user_id, question.account_id, "next"),
                dedup_key=next_key(question.question_id),
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
            outcome = await self._closed_outcome(question, answer, note)
            await self._interactive.show_choice(question.message_ref, outcome)
        except Exception as exc:
            logger.warning(
                f"[FactAnswer] message of question {question.question_id} not re-rendered on "
                f"{question.message_ref.platform}: {type(exc).__name__}: {exc}"
            )

    async def _closed_outcome(
        self, question: FactReviewQuestion, answer: Optional[AnswerKind], note: Optional[UIMessage],
    ) -> ChoiceOutcome:
        """The question's message without controls: the chosen option for an answer, else `note` (default
        `MEMORY_QUESTION_CLOSED`), the texts and the shown quote from one read (`question_and_labels`)."""
        message, labels = await self._texts.question_and_labels(question, question.user_id, question.account_id)
        if answer is not None:
            return ChoiceOutcome(message=message, chosen_option_id=option_id(answer))
        return ChoiceOutcome(message=message, note=labels[(note or UIMessage.MEMORY_QUESTION_CLOSED).value])

    async def _kept_outcome(self, question: FactReviewQuestion, note: UIMessage) -> ChoiceOutcome:
        """The open question's message with `note` shown and its options kept, texts and the shown quote from one read (`question_and_labels`)."""
        message, labels = await self._texts.question_and_labels(question, question.user_id, question.account_id)
        return ChoiceOutcome(message=message, note=labels[note.value], keep_options=True)

    async def _asked_fact(
        self, question: FactReviewQuestion, accepts: AbstractSet[FactState],
    ) -> Optional[StoredFact]:
        """The fact the question is about as it stands now, followed forward as `accepts` allows: None when it is gone,
        its chain ends elsewhere, or a newer document has a text the owner never saw (`walked_to_unseen_text`)."""
        try:
            stored = await self._repo.resolve_current(question.fact_id, question.account_id, accepts)
        except FactResolutionError as exc:
            logger.info(
                f"[FactAnswer] question {question.question_id}: fact {question.fact_id[:8]} cannot be followed: {exc}"
            )
            return None
        if stored is None or walked_to_unseen_text(question.quoted_text, stored.fact, question.fact_id):
            logger.info(
                f"[FactAnswer] question {question.question_id}: fact {question.fact_id[:8]} changed since it was asked"
            )
            return None
        return stored

    async def _question(self, question_id: str) -> Optional[FactReviewQuestion]:
        return next(iter(await self._review_store.get_questions([question_id])), None)

    async def _append_reply(self, question: FactReviewQuestion, user_text: str, model_text: str) -> None:
        """The owner's reply and its acknowledgement as one pair in the question's session, raising when not stored."""
        if question.session_id is None:
            raise ValueError(f"question {question.question_id} has no session to append to")
        await self._session_store.append_messages_batch(
            session_id=question.session_id,
            owner_id=question.user_id,
            messages=[
                Message(role="user", parts=[MessagePart(text=user_text)]),
                Message(role="model", parts=[MessagePart(text=model_text, full_text=model_text)]),
            ],
            raise_on_error=True,
        )

    async def _not_applied(self, question: FactReviewQuestion, now: datetime, why: str) -> AnswerResult:
        logger.warning(f"[FactAnswer] question {question.question_id}: the answer's write was refused ({why})")
        await self._count(question.account_id, now, {CounterField.ANSWERS_NOT_APPLIED: 1})
        return AnswerResult.NOT_APPLIED

    async def _note_set_aside(self, account_id: str, now: datetime) -> None:
        """A fact was set aside: the account's next digest has something to tell (`undigested_since`, the first instant
        kept). Best effort: the answer is written, so a failure here is a WARNING and never the answer's."""
        try:
            if not await self._review_store.mark_undigested(account_id, now):
                logger.warning(f"[FactAnswer] account {account_id[:8]} has no review state; undigested_since not set")
        except Exception as exc:
            logger.warning(f"[FactAnswer] undigested_since of account {account_id[:8]} not set: {type(exc).__name__}: {exc}")

    async def _count(self, account_id: str, now: datetime, counts: Dict[CounterField, int]) -> None:
        try:
            await self._counters.add(account_id, counter_day(now), counts)
        except Exception as exc:
            logger.warning(
                f"[FactAnswer] {', '.join(c.value for c in counts)} not counted for account {account_id[:8]}: "
                f"{type(exc).__name__}"
            )
