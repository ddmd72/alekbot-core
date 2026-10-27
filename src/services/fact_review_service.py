"""FactReviewService — one memory-review run for an account (Facts S4 plan §3.2, §3.7).

`run(user_id, account_id, mode)` is the whole run: the owner check, then, for `daily`, the setup, the account's
memory-write lease (the one `ConsolidationService` holds, so a review and a consolidation never write at once),
housekeeping of questions whose send is over, the review pass, the send of the next question, the biographical cache,
the digest, the counters and the state marks. `next` only sends the next question (no lease, no model).

The clock is read once per run; that `now` is every step's and every domain decision's. The one other read is the lease
budget at routing time (as `ConsolidationService`): the agent's timeout is what is left of the lease then, so the
pass ends before the lease does however long the steps before it took. Every question close goes through
`FactAnswerService.close_question`; nothing here closes one itself.

Built: steps 1-15 and the failure rule (T8a, T8b, T8c, T8d1, T8d2).
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from ..domain.consolidation import consolidation_agent_id
from ..domain.entities import FactEntity, FactState
from ..domain.exceptions import FactResolutionError, FactWriteConflict
from ..domain.fact_lifecycle import REVIEW_ACCEPTS
from ..domain.fact_lifecycle_alerts import revive_alert
from ..domain.fact_lifecycle_day import CounterField, counter_day
from ..domain.fact_review_after_pass import after_pass
from ..domain.fact_review_ask import AskRequest
from ..domain.fact_review_answers import history_fallback
from ..domain.fact_review_digest import (
    DIGEST_LIMIT, digest_day, digest_facts, digest_histories, digest_invoices, digest_since, history_line, pass_set_aside,
)
from ..domain.fact_review_frame import NOT_DELIVERED_REPLY, not_delivered_frame, question_text, review_frame
from ..domain.fact_review_language import language_to_store
from ..domain.fact_review_invoice import InvoiceRequest
from ..domain.fact_review_question import ClosedBy, FactReviewQuestion
from ..domain.fact_review_quote_translation import QuoteTranslation
from ..domain.fact_review_reasons import superseded_reason
from ..domain.fact_review_result import decode_review_result
from ..domain.fact_review_rules import (
    QUEUE_ACCEPTS,
    asked_fact_unchanged,
    becomes_unanswered,
    channel_refusal,
    digest_due,
    digest_question,
    digest_question_expired,
    due_timeouts,
    fallback_fields,
    fallback_sets_aside,
    fallback_writes_history,
    kept_twice,
    kept_twice_fields,
    kept_twice_question,
    offered,
    queued_fields,
    queued_question,
    revive_alert_due,
    revives_in_window,
    review_close_source,
    select_batch,
    stuck_pending,
    timeout_candidate,
    unread_replies,
)
from ..domain.fact_review_state import FactReviewState
from ..domain.fact_update import ParsedFactUpdate
from ..domain.fact_update_outcome import UpdateRefusal
from ..domain.fact_write_context import FactWriteContext
from ..domain.fact_review_task import (
    NEIGHBOUR_QUERY,
    FactReviewTask,
    ReviewItem,
    encode_review_task,
    owner_message_texts,
    owner_messages_block,
    questions_about,
    review_item,
)
from ..domain.fact_review_task_keys import (
    FACT_REVIEW_TASK,
    REVIEW_DEADLINE_S,
    REVIEW_MODES,
    ReviewMode,
    defer_key,
    review_payload,
)
from ..domain.llm import Message, MessagePart
from ..domain.local_time import local_date
from ..domain.messaging import session_id_for
from ..domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, LEASE_MARGIN, agent_timeout_s, lease_until
from ..domain.notification import NotificationChannel
from ..domain.owner_timezone import not_owner_reason
from ..domain.pass_actor import PassActor
from ..domain.request_context import RequestContext
from ..domain.stored_fact import StoredFact
from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.channel_binding_port import ChannelBindingPort
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
from ..ports.fact_update_port import FactUpdatePort
from ..ports.interactive_message_port import InteractiveMessagePort
from ..ports.memory_write_lease import MemoryWriteLeasePort
from ..ports.notification_state_port import NotificationStatePort
from ..ports.repository import FactRepository
from ..ports.session_store import SessionStore
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..utils.logger import logger

if TYPE_CHECKING:
    from .fact_answer_service import FactAnswerService
    from .fact_question_texts import FactQuestionTexts
    from .owner_timezone_resolver import OwnerTimezoneResolver
    from .user_notification_service import UserNotificationService

_MARGIN_S = LEASE_MARGIN.total_seconds()
# A history fallback that fails this many times closes its question `resolved_elsewhere` (C9).
_MAX_FALLBACK_ATTEMPTS = 3
_SENDER = "fact_review_service"
# The consolidation agent's task name (`Intent.REVIEW_STALE_FACTS`; a literal, as ConsolidationService's own task names,
# because services do not import infrastructure).
_REVIEW_TASK = "review_stale_facts"
# What a queue write is built from the fact as read: its `queued` record and its in-place write (given the question id).
_QueueBuild = Callable[[FactEntity, str], Tuple[FactReviewQuestion, Dict[str, Any]]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _FactWrites:
    """Whether this run wrote a fact, for the one biographical-cache refresh (the agent refreshes after its own writes).
    A step calls `wrote()` at once after each fact write lands, before anything else can raise, so a step that writes
    and then fails still gets its refresh (in the run's `finally`)."""

    def __init__(self) -> None:
        self.any = False
        self.refreshed = False

    def wrote(self) -> None:
        self.any = True


@dataclass(frozen=True)
class _StepResult:
    """What one step counts, and the facts it brought back to current (`revived`, for the revive alert of step 15). Its
    fact writes are not here: they are marked on the run's `_FactWrites` as they land."""

    counts: Dict[CounterField, int] = field(default_factory=dict)
    revived: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _Timeouts(_StepResult):
    """Step 6: besides the counts, the timed-out questions' facts that join this run's batch."""

    candidates: Tuple[FactEntity, ...] = ()


@dataclass(frozen=True)
class _Fallback:
    """What one timed-out question's fallback came to: `written_id` (None: nothing landed), `wrote_history` and
    `not_applied` (a history fallback that failed and was counted)."""

    written_id: Optional[str] = None
    wrote_history: bool = False
    not_applied: int = 0


@dataclass
class _Tally:
    """The run's counts and revived facts, from every step."""

    counts: Dict[CounterField, int] = field(default_factory=dict)
    revived: List[str] = field(default_factory=list)

    def add(self, step: _StepResult) -> None:
        for name, value in step.counts.items():
            self.counts[name] = self.counts.get(name, 0) + value
        self.revived.extend(f for f in step.revived if f not in self.revived)

    def count(self, name: CounterField, value: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + value


class FactReviewService:
    """`run(user_id, account_id, mode="daily") -> {"status": ...}`; ports only.

    Dependencies, by the step that reads them:
    - `account_repo`, `user_repo`: the owner check (step 1);
    - `agent_factory`, `owner_zones`: the setup (step 3);
    - `texts`: whether the language mirrors the owner (step 10), the send's message (step 12), the digest's lines (T8d2);
    - `lease`, `task_queue`, `run_timeout_s`: the lease and the deferral (step 4, and step 10 when no lease time is
      left); `task_queue` also takes the next question's release through `answer_service`;
    - `review_store`, `session_store`: housekeeping (step 5) and every question and state write; `session_store` also
      gives the owner's recent messages (step 10, mirror mode);
    - `repo`: the timed-out questions' facts and their neighbours, the stale facts, the prefetch and the service's fact
      writes (steps 6-8, 10, 11), the biographical cache (step 13), the revive alert's read (step 15);
    - `answer_service`: every question close (steps 6, 11);
    - `coordinator`: the review pass (step 10);
    - `notification_state`, `channel_bindings`, `interactive`: the send's channel and post (step 12);
    - `notifications`: the owner's channel for his recent messages (step 10), the digest (T8d2);
    - `alert_sink`: the revive alert (step 15);
    - `counters`: the day's counters (step 15);
    - `fact_updates`: the typed fact write of a history fallback (step 6, the timeout path), of the folds an ask supersedes
      and of nothing else (step 11);
    - `cabinet_url`: where the digest's invoices block sends the owner;
    - `clock`: read once per run, and once more for the lease budget at routing time.
    """

    def __init__(
        self,
        repo: FactRepository,
        review_store: FactReviewStore,
        counters: FactLifecycleCountersPort,
        lease: MemoryWriteLeasePort,
        coordinator: Any,  # AgentCoordinator — typed as Any to avoid an infrastructure import, as ConsolidationService
        agent_factory: Any,  # UserAgentFactory
        user_repo: UserRepository,
        account_repo: AccountRepository,
        texts: "FactQuestionTexts",
        owner_zones: "OwnerTimezoneResolver",
        notification_state: NotificationStatePort,
        channel_bindings: ChannelBindingPort,
        notifications: "UserNotificationService",
        session_store: SessionStore,
        task_queue: TaskQueue,
        answer_service: "FactAnswerService",
        interactive: InteractiveMessagePort,
        alert_sink: AlertSinkPort,
        run_timeout_s: float,
        clock: Callable[[], datetime] = _utcnow,
        *,
        fact_updates: FactUpdatePort,
        cabinet_url: str,
    ) -> None:
        if run_timeout_s <= _MARGIN_S:
            raise ValueError(f"run_timeout_s must exceed the lease margin ({_MARGIN_S:.0f} s), got {run_timeout_s}")
        self._repo = repo
        self._review_store = review_store
        self._counters = counters
        self._lease = lease
        self._coordinator = coordinator
        self._agent_factory = agent_factory
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._texts = texts
        self._owner_zones = owner_zones
        self._notification_state = notification_state
        self._channel_bindings = channel_bindings
        self._notifications = notifications
        self._session_store = session_store
        self._task_queue = task_queue
        self._answer_service = answer_service
        self._interactive = interactive
        self._alert_sink = alert_sink
        self._run_timeout_s = run_timeout_s
        self._clock = clock
        self._fact_updates = fact_updates
        self._cabinet_url = cabinet_url

    async def run(self, user_id: str, account_id: str, mode: ReviewMode = "daily") -> Dict[str, Any]:
        """One run; every result is 200-shaped.

        `not_owner`: `user_id` is not the account's first owner, or the user's profile is of another account (one
        WARNING; nothing else read or written). `next`: the send only, then `completed`. `daily`: `deferred` when the
        lease is held (one named task per account and bucket), or when less than the lease margin was left for the pass
        at routing time (the same task; nothing was routed and no mark is set, the send and the digest still run);
        else `completed`, or `failed` when the review pass failed, a coordinator exception included (`review_failed`,
        `mark_failed`; the send and the digest still run and every write stands). A lease, store or queue error raises.
        Raises ValueError for a mode that is neither."""
        if mode not in REVIEW_MODES:
            raise ValueError(f"{mode!r} is not a review mode")
        now = self._clock()
        reason = await self._not_owner(user_id, account_id)
        if reason is not None:
            logger.warning(
                f"[FactReview] user {user_id[:8]} is not the owner of account {account_id[:8]} ({reason}); nothing done"
            )
            return {"status": "not_owner"}
        if mode == "next":
            return await self._next(user_id, account_id, now)
        return await self._daily(user_id, account_id, now)

    # ------------------------------------------------------------------------------------------------ steps 1-4

    async def _not_owner(self, user_id: str, account_id: str) -> Optional[str]:
        account = await self._account_repo.get_account(account_id)
        profile = await self._user_repo.get_user(user_id)
        return not_owner_reason(account, user_id, profile)

    async def _next(self, user_id: str, account_id: str, now: datetime) -> Dict[str, Any]:
        """Step 2: the send only. No lease, no model; the run's marks are the daily run's, not this one's."""
        zone = (await self._owner_zones.resolve(account_id)).zone
        tally = _Tally()
        writes = _FactWrites()
        try:
            tally.add(await self._send_next(user_id, account_id, now, zone, writes))
            await self._refresh_once(account_id, writes)
            await self._count(account_id, now, tally)
            return {"status": "completed"}
        finally:
            await self._refresh_after_error(account_id, writes)

    async def _daily(self, user_id: str, account_id: str, now: datetime) -> Dict[str, Any]:
        await self._agent_factory.ensure_agents_for_user(user_id)
        zone = (await self._owner_zones.resolve(account_id)).zone
        owner = uuid.uuid4().hex
        until = lease_until(now, self._run_timeout_s)
        if not await self._lease.acquire(account_id, owner, now, until):
            await self._defer(user_id, account_id, now)
            return {"status": "deferred"}
        try:
            await self._mark("started", self._review_store.mark_started, account_id, now)
            return await self._leased(user_id, account_id, now, zone, until)
        finally:
            await self._release(account_id, owner)

    async def _defer(self, user_id: str, account_id: str, now: datetime) -> None:
        await self._task_queue.enqueue_worker_task(
            FACT_REVIEW_TASK,
            review_payload(user_id, account_id, "daily"),
            delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
            deadline_seconds=REVIEW_DEADLINE_S,
            dedup_key=defer_key(account_id, now),
        )
        logger.info(f"⏳ [FactReview] memory-write lease busy: deferred the review of account {account_id[:8]}")

    async def _release(self, account_id: str, owner: str) -> None:
        try:
            await self._lease.release(account_id, owner)
        except Exception as exc:
            logger.warning(
                f"[FactReview] releasing the memory-write lease failed for account {account_id[:8]} "
                f"(it expires on its own): {exc}"
            )

    # ------------------------------------------------------------------------------------------------ the leased run

    async def _leased(
        self, user_id: str, account_id: str, now: datetime, zone: str, until: datetime,
    ) -> Dict[str, Any]:
        tally = _Tally()
        writes = _FactWrites()
        try:
            await self._housekeeping(account_id, now, zone)
            state = await self._review_store.get_state(account_id)
            questions = await self._review_store.questions_for_account(account_id)
            timeouts = await self._timeouts(user_id, account_id, questions, now, zone, writes)
            tally.add(timeouts)
            tally.add(await self._kept_twice(user_id, account_id, state, questions, now, zone, writes))
            await self._settle_digest(account_id, questions, now)
            # Steps 6 and 7 close and create questions and change offers: the batch is chosen from what is stored now.
            state = await self._review_store.get_state(account_id)
            questions = await self._review_store.questions_for_account(account_id)
            batch = await self._select(account_id, state, questions, timeouts.candidates, now)
            failed = deferred = False
            if batch:  # step 9: nothing selected, no model call
                response = await self._route(user_id, account_id, batch, questions, now, zone, until)
                if response is None:  # no lease time was left: the run is deferred, nothing was routed
                    deferred = True
                elif response.status is not AgentStatus.SUCCESS:
                    failed = True
                    logger.warning(
                        f"[FactReview] the review pass failed for account {account_id[:8]}: {response.error}"
                    )
                else:
                    tally.add(await self._after_pass(user_id, account_id, batch, response, now, zone, writes))
            tally.add(await self._send_next(user_id, account_id, now, zone, writes))  # step 12
            await self._refresh_once(account_id, writes)  # step 13
            await self._digest(user_id, account_id, now, zone)  # step 14
            if failed:
                tally.count(CounterField.REVIEW_FAILED)
            await self._count(account_id, now, tally)  # step 15
            await self._revive_alert(account_id, tally.revived, now)
            if deferred:
                return {"status": "deferred"}
            if failed:
                await self._mark("failed", self._review_store.mark_failed, account_id, now)
                return {"status": "failed"}
            await self._mark("succeeded", self._review_store.mark_succeeded, account_id, now)
            return {"status": "completed"}
        finally:
            await self._refresh_after_error(account_id, writes)

    async def _housekeeping(self, account_id: str, now: datetime, zone: str) -> None:
        """Step 5. A `stuck_pending` question with a message ref was delivered: it becomes asked at its `appended_at`. One
        without a ref is retracted (the not-delivered pair, only when its frame was appended) and goes back to the front
        of the queue. Then an asked question from an earlier owner-local day becomes unanswered and stops being current.
        A send claimed today is left alone: it may still be running. Writes no fact."""
        state = await self._review_store.get_state(account_id) or FactReviewState(account_id=account_id)
        questions: List[FactReviewQuestion] = []
        for question in await self._review_store.questions_for_account(account_id):
            if stuck_pending(question, state, now, zone):
                question = await self._settle_stuck(question)
            questions.append(question)
        for question in questions:
            if becomes_unanswered(question, now, zone):
                if await self._review_store.mark_unanswered(question.question_id):
                    logger.info(f"[FactReview] question {question.question_id} unanswered since an earlier day")
                else:
                    logger.warning(f"[FactReview] question {question.question_id} not marked unanswered: it changed")

    async def _settle_stuck(self, question: FactReviewQuestion) -> FactReviewQuestion:
        """The question as housekeeping left it: completed, requeued, or as read when the store refused the move."""
        if question.delivered:
            if await self._review_store.complete_delivered(question.question_id):
                logger.info(f"[FactReview] question {question.question_id} was delivered: completed as asked")
                return question.completed()
        else:
            await self._retract(question)
            if await self._review_store.requeue_front(question.question_id, failed_today=False):
                logger.info(f"[FactReview] question {question.question_id} was never posted: back at the queue's front")
                return question.requeued()
        logger.warning(f"[FactReview] stuck question {question.question_id} not settled: it changed meanwhile")
        return question

    async def _retract(self, question: FactReviewQuestion) -> None:
        """The not-delivered pair after a question whose frame was appended and whose post never succeeded
        (`retraction_due`), once per attempt: the question is marked retracted right after the pair lands, so a retry
        that finds it still pending (its requeue failed) appends no second pair. A crash between the pair and the mark
        can still repeat it. A question not due for one gets nothing."""
        if not question.retraction_due:
            return
        await self._append_pair(question, not_delivered_frame(question.question_id), NOT_DELIVERED_REPLY)
        if not await self._review_store.mark_retracted(question.question_id):
            logger.warning(f"[FactReview] question {question.question_id}: not marked retracted (it changed meanwhile)")

    async def _append_pair(self, question: FactReviewQuestion, user_text: str, model_text: str) -> None:
        """A user/model pair in the question's session, raising when it was not stored."""
        if question.session_id is None:
            raise ValueError(f"question {question.question_id} has no session to append to")
        try:
            await self._session_store.append_messages_batch(
                session_id=question.session_id,
                owner_id=question.user_id,
                messages=[
                    Message(role="user", parts=[MessagePart(text=user_text)]),
                    Message(role="model", parts=[MessagePart(text=model_text, full_text=model_text)]),
                ],
                raise_on_error=True,
            )
        except Exception:
            logger.error(f"❌ [FactReview] pair of question {question.question_id} not appended", exc_info=True)
            raise

    @staticmethod
    async def _mark(
        what: str, mark: Callable[[str, datetime], Awaitable[bool]], account_id: str, now: datetime,
    ) -> None:
        """A state mark; an account with no review state document (the janitor's enqueue creates it) is a WARNING."""
        if not await mark(account_id, now):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; not marked {what}")

    async def _refresh_once(self, account_id: str, writes: _FactWrites) -> None:
        """Step 13: once, when this service wrote a fact. A refresh that raises here is not tried again."""
        if writes.any and not writes.refreshed:
            writes.refreshed = True
            await self._repo.refresh_biographical_context_cache(owner_id=account_id)

    async def _refresh_after_error(self, account_id: str, writes: _FactWrites) -> None:
        """The `finally` of a run: a run cut short by an error after a fact write still refreshes, once; a refresh
        error there is logged, so it never hides the run's own error."""
        try:
            await self._refresh_once(account_id, writes)
        except Exception as exc:
            logger.error(
                f"[FactReview] biographical cache of account {account_id[:8]} not refreshed after the review's "
                f"writes: {type(exc).__name__}: {exc}"
            )

    async def _count(self, account_id: str, now: datetime, tally: _Tally) -> None:
        """Step 15's counters; best effort, as every counter of the review: a failure is a WARNING."""
        counts = {name: value for name, value in tally.counts.items() if value}
        if not counts:
            return
        try:
            await self._counters.add(account_id, counter_day(now), counts)
        except Exception as exc:
            logger.warning(
                f"[FactReview] {', '.join(c.value for c in counts)} not counted for account {account_id[:8]}: "
                f"{type(exc).__name__}"
            )

    # ------------------------------------------------------------------------------------------------ steps 6-7

    async def _timeouts(
        self,
        user_id: str,
        account_id: str,
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
        writes: _FactWrites,
    ) -> _Timeouts:
        """Step 6 (§3.8), over every question `due_timeouts` names. Its fact rewritten, closed or gone meanwhile
        (`asked_fact_unchanged`): the question closes `resolved_elsewhere`. Else the check of S4-D19 (`timeout_candidate`,
        zero LLM: a reply in words not yet shown, or a newer neighbour): a candidate joins this run's batch (`candidates`; the pass sees its question as
        timed out, and `after_pass` gives it the fallback if it stays undecided); every other one gets its fallback now
        (`_fallbacks`, the one fallback path). So no timed-out question that is not a candidate is left open for the
        batch, except one whose fallback write conflicted (the next run's) and one whose `history` fallback was refused or
        failed (it stays open with `fallback_attempts` counted until `_history_failed` closes it)."""
        candidates: List[FactEntity] = []
        due: List[FactReviewQuestion] = []
        for question in due_timeouts(questions, now):
            stored = await self._asked_fact(account_id, question, "timed out")
            if stored is None:
                if await self._answer_service.close_question(question, ClosedBy.RESOLVED_ELSEWHERE):
                    logger.info(f"[FactReview] question {question.question_id} timed out; its fact was settled elsewhere")
                continue
            if timeout_candidate(stored.fact, question, await self._nearest(user_id, account_id, stored.fact)):
                logger.info(f"[FactReview] question {question.question_id} timed out; an unread reply or a newer neighbour sends it to the review")
                candidates.append(stored.fact)
            else:
                due.append(question)
        settled = await self._fallbacks(account_id, due, now, zone, writes)
        return _Timeouts(counts=settled.counts, revived=settled.revived, candidates=tuple(candidates))

    async def _kept_twice(
        self,
        user_id: str,
        account_id: str,
        state: Optional[FactReviewState],
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
        writes: _FactWrites,
    ) -> _StepResult:
        """Step 7 (S4-D20): every stale fact `kept_twice` names gets a code question and is queued through `_queue` (the
        one queue write), with `kept_twice_fallback` and `kept_twice_fields`: made current while it waits, never revived.
        A fact whose queue write landed loses its `offers` entry; one that conflicted keeps it for the next run."""
        if state is None or not state.offers:
            return _StepResult()
        def build(stored: FactEntity, question_id: str) -> Tuple[FactReviewQuestion, Dict[str, Any]]:
            return (
                kept_twice_question(stored, question_id, user_id, at=now),
                kept_twice_fields(stored, question_id, now),
            )

        queued: List[str] = []
        for fact in kept_twice(await self._repo.get_stale_facts(account_id), state.offers, questions):
            if await self._queue(account_id, fact.id, build, writes):
                queued.append(fact.id)
        if queued and not await self._review_store.mark_removed(account_id, queued):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; offers not cleared")
        if queued:
            logger.info(f"[FactReview] {len(queued)} facts of account {account_id[:8]} kept twice: queued for the owner")
        return _StepResult(counts={CounterField.QUEUED: len(queued)} if queued else {})

    async def _settle_digest(self, account_id: str, questions: Sequence[FactReviewQuestion], now: datetime) -> None:
        """Housekeeping of the Cabinet-only questions (`delivery == "digest"`, P2-2): one unanswered `DIGEST_QUESTION_DAYS`
        (`digest_question_expired`), or whose fact moved on (`_asked_fact`), closes `resolved_elsewhere`. Writes no fact."""
        for question in questions:
            if question.in_chat:
                continue
            if digest_question_expired(question, now):
                why = "unanswered too long"
            elif await self._asked_fact(account_id, question, "digest question") is None:
                why = "its fact moved on"
            else:
                continue
            if await self._answer_service.close_question(question, ClosedBy.RESOLVED_ELSEWHERE):
                logger.info(f"[FactReview] digest question {question.question_id} closed: {why}")

    async def _nearest(self, user_id: str, account_id: str, fact: FactEntity) -> List[FactEntity]:
        """The current facts nearest to `fact` by its vector (its own document among them), nearest first; none without
        a vector."""
        if not fact.vector:
            return []
        return await self._repo.search_facts(list(fact.vector), limit=NEIGHBOUR_QUERY, user_id=user_id, account_id=account_id)

    async def _asked_fact(self, account_id: str, question: FactReviewQuestion, why: str) -> Optional[StoredFact]:
        """The question's fact as stored now, when it is still the document asked about (`asked_fact_unchanged`); else
        None, logged at INFO with `why` the fact was read."""
        try:
            stored = await self._repo.resolve_current(question.fact_id, account_id, REVIEW_ACCEPTS)
        except FactResolutionError as exc:
            stored, detail = None, str(exc)
        else:
            detail = "it is gone or changed"
        if not asked_fact_unchanged(question, stored.fact if stored is not None else None):
            logger.info(f"[FactReview] question {question.question_id} ({why}): its fact moved on ({detail})")
            return None
        return stored

    # ------------------------------------------------------------------------------------------------ step 12: send

    async def _send_next(
        self, user_id: str, account_id: str, now: datetime, zone: str, writes: _FactWrites,
    ) -> _StepResult:
        """Step 12 and `mode="next"` (§3.7): one question to the owner's primary channel, when one may go.

        The channel (`channel_refusal`): none usable → nothing sent, INFO. The claim (`claim_next`, deciding with the
        domain's `may_send` in its transaction) for the owner-local day of `now`: none → nothing. The message in the
        message language (`texts.message_for`), then the pair (`review_frame` + `question_text`, raising when not
        stored): a failure puts the question back at the queue's front (`sent` kept, no failed day). The post: the
        returned ref is stored (`set_message_ref`) before the question is asked (`mark_asked`), so a crash in between is
        completed by housekeeping, never resent. A post that raises is not delivered: the not-delivered pair
        (`_retract`), the question back at the front with today as the failed day, `not_delivered` counted. A question
        closed meanwhile (the Cabinet, the review) stays closed; its close released the next one, so nothing is
        released here. Writes no fact (`writes` is not marked); store errors raise."""
        today = local_date(now, zone)
        channel = await self._question_channel(user_id, account_id)
        if channel is None:
            return _StepResult()
        claimed = await self._review_store.claim_next(account_id, today, session_id_for(user_id, channel.channel_id))
        if claimed is None:
            return _StepResult()
        qid = claimed.question_id
        try:
            message = await self._texts.message_for(claimed, user_id, account_id)
        except Exception:
            await self._requeue(claimed, failed_today=False, why="its message could not be built")
            raise
        try:
            await self._append_pair(claimed, review_frame(qid), question_text(message))
        except Exception:
            await self._requeue(claimed, failed_today=False, why="its pair was not appended")
            return _StepResult()
        if not await self._review_store.mark_appended(qid, now):
            # Closed between the claim and here: the owner is not sent it, and the history says so.
            logger.info(f"[FactReview] question {qid} was closed before it was posted; not sent")
            await self._append_pair(claimed, not_delivered_frame(qid), NOT_DELIVERED_REPLY)
            return _StepResult()
        appended = claimed.appended(now)
        try:
            ref = await self._interactive.post(channel.platform, channel.channel_id, message)
        except Exception as exc:
            logger.warning(f"[FactReview] question {qid} not delivered on {channel.platform}: {type(exc).__name__}: {exc}")
            await self._retract(appended)
            await self._requeue(appended, failed_today=True, why="it was not delivered")
            return _StepResult(counts={CounterField.NOT_DELIVERED: 1})
        if not await self._review_store.set_message_ref(qid, ref):
            logger.warning(f"[FactReview] question {qid} was posted at {ref.message_id} but closed meanwhile; ref not kept")
            return _StepResult(counts={CounterField.QUESTIONS_SENT: 1})
        if await self._review_store.mark_asked(qid, now):
            logger.info(f"📨 [FactReview] question {qid} sent to account {account_id[:8]} on {channel.platform}")
        else:
            logger.info(f"[FactReview] question {qid} was posted and closed meanwhile; it stays closed")
        return _StepResult(counts={CounterField.QUESTIONS_SENT: 1})

    async def _question_channel(self, user_id: str, account_id: str) -> Optional[NotificationChannel]:
        """The owner's primary channel when a question may go there (`channel_refusal`), else None (INFO, every run:
        without a usable channel no question becomes current, so no close releases a `next` run and only the daily run
        gets here)."""
        primary = await self._notification_state.get_primary(user_id)
        binding = await self._channel_bindings.get(primary.channel_id) if primary is not None else None
        refusal = channel_refusal(primary, binding)
        if refusal is not None:
            logger.info(f"[FactReview] no question sent to account {account_id[:8]}: {refusal}; the queue waits")
            return None
        return primary

    async def _requeue(self, question: FactReviewQuestion, failed_today: bool, why: str) -> None:
        if await self._review_store.requeue_front(question.question_id, failed_today=failed_today):
            logger.info(f"[FactReview] question {question.question_id} back at the queue's front: {why}")
        else:
            logger.warning(f"[FactReview] question {question.question_id} not requeued ({why}): it changed meanwhile")

    # ------------------------------------------------------------------------------------------------ step 14: digest

    async def _digest(self, user_id: str, account_id: str, now: datetime, zone: str) -> None:
        """Step 14 (§3.11): the weekly digest when `digest_due`. The owner's local week is claimed first (one digest per
        ISO week); then the facts closed since the last digest (`digest_since`, `DIGEST_LIMIT`) that the digest lists
        (`digest_facts`), the facts written as history by a timeout since then and the open invoice questions are sent as
        one message in the message language, `notify_raw`, which writes no history. Then
        `last_digest_at = now` and `undigested_since` cleared. Nothing to list: the week is still claimed and the marks
        still move, and no message is sent (the account is not asked again until something else is set aside).

        Best effort, like the revive alert: it runs after the pass's own work, so an error is logged at ERROR and ends the
        digest, never the run. A digest that was not delivered (no usable channel, or the post failed) or whose read
        failed releases its claim and leaves the marks, so the next run tries again; one that was sent but could not be
        marked keeps the claim, so the owner is not sent it twice."""
        try:
            state = await self._review_store.get_state(account_id)
            if state is None or not digest_due(state, now):
                return
            day = digest_day(now, zone)
            if not await self._review_store.claim_digest(user_id, day, now):
                logger.info(f"[FactReview] the digest of account {account_id[:8]} was already claimed this week")
                return
            try:
                listed = await self._send_digest(user_id, account_id, state, now, zone)
            except Exception:
                await self._release_digest(user_id, day)
                raise
            if listed is None:
                await self._release_digest(user_id, day)
                logger.warning(f"[FactReview] the digest of account {account_id[:8]} was not delivered; tried again next run")
                return
            if not await self._review_store.mark_digest(account_id, now):
                logger.warning(f"[FactReview] account {account_id[:8]} has no review state; digest not marked")
            logger.info(f"📬 [FactReview] digest of account {account_id[:8]}: {listed} entries")
        except Exception as exc:
            logger.error(
                f"❌ [FactReview] the digest of account {account_id[:8]} failed: {type(exc).__name__}: {exc}", exc_info=True,
            )

    async def _send_digest(
        self, user_id: str, account_id: str, state: FactReviewState, now: datetime, zone: str,
    ) -> Optional[int]:
        """The digest's read and post: how many entries it listed (removed facts, histories written by silence and open
        invoice questions; 0: nothing to list, nothing sent), or None when its message was not delivered."""
        since = digest_since(state, now)
        closed = await self._repo.get_recently_closed(account_id, since=since, limit=DIGEST_LIMIT)
        listed = digest_facts(closed)
        histories = digest_histories(await self._review_store.timeout_histories(account_id), since)
        invoices = digest_invoices(await self._review_store.questions_for_account(account_id))
        if not (listed or histories or invoices):
            return 0
        text = await self._texts.digest_for(
            user_id, account_id, listed,
            histories=[history_line(q, zone) for q in histories],
            invoices=[q.quoted_text for q in invoices],
            cabinet_url=self._cabinet_url,
        )
        if not await self._notifications.notify_raw(user_id, account_id, text, literal=True):
            return None
        return len(listed) + len(histories) + len(invoices)

    async def _release_digest(self, user_id: str, day: date) -> None:
        try:
            await self._review_store.release_digest(user_id, day)
        except Exception as exc:
            logger.warning(f"[FactReview] the digest claim of user {user_id[:8]} not released: {type(exc).__name__}: {exc}")

    async def _note_set_aside(self, account_id: str, now: datetime) -> None:
        """A fact was archived or invalidated in this run: the next digest has something to tell (`undigested_since`, the
        first instant kept)."""
        if not await self._review_store.mark_undigested(account_id, now):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; undigested_since not set")

    # ------------------------------------------------------------------------------------------------ step 8: select

    async def _select(
        self,
        account_id: str,
        state: Optional[FactReviewState],
        questions: Sequence[FactReviewQuestion],
        candidates: Sequence[FactEntity],
        now: datetime,
    ) -> List[FactEntity]:
        """Step 8, the batch (`select_batch`): the facts of questions with a reply the review has not read, the
        timed-out candidates, then the account's stale facts without an open question, by offers and age."""
        stale = await self._repo.get_stale_facts(account_id)
        replies = [fact for q in unread_replies(questions) if (fact := await self._fact_of(account_id, q)) is not None]
        return select_batch(stale, questions, replies, candidates, state.offers if state is not None else {})

    async def _fact_of(self, account_id: str, question: FactReviewQuestion) -> Optional[FactEntity]:
        """The document the question is about, read whole; None (INFO) when its fact moved on (closed, rewritten, gone):
        the question's timeout then settles it (step 6)."""
        stored = await self._asked_fact(account_id, question, "not reviewed with its reply")
        return stored.fact if stored is not None else None

    # ------------------------------------------------------------------------------------------------ step 10: route

    async def _route(
        self,
        user_id: str,
        account_id: str,
        batch: Sequence[FactEntity],
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
        until: datetime,
    ) -> Optional[AgentResponse]:
        """Step 10: the prefetch (§3.4), then the lease budget read now; less than the margin left defers the run (as
        `ConsolidationService`) and routes nothing (None). The task carries the fixed message language the pass translates
        into (not in mirror mode). Else `offers` (+1 per stale batch fact) and `offered_at` (each
        reply shown) are written and the agent task is routed inside `RequestContext`, its timeout the budget. A
        coordinator exception is the pass's failure (logged, a failed response): the failure rule, not a 5xx. The pass
        is told whether the language mirrors the owner (`mirror`); the agent words its own request."""
        async with RequestContext(user_id=user_id, account_id=account_id):
            task = await self._task(user_id, account_id, batch, questions, now)
            budget = agent_timeout_s(until, self._clock())  # F2: the lease left now, not at the run's start
            if budget < _MARGIN_S:
                logger.warning(
                    f"[FactReview] {budget:.0f} s of lease left for the review pass of account {account_id[:8]}; deferred"
                )
                await self._defer(user_id, account_id, now)
                return None
            await self._offer(account_id, batch, task.questions, now)
            message = AgentMessage(
                task_id=uuid.uuid4().hex,
                sender=_SENDER,
                recipient=consolidation_agent_id(user_id),
                intent=AgentIntent.DELEGATE,
                payload={"task": _REVIEW_TASK, **encode_review_task(task)},
                context={
                    "user_id": user_id, "account_id": account_id, "owner_timezone": zone,
                    "suppress_transient_retry": True,
                },
                timeout_ms=int(budget * 1000),
            )
            logger.info(
                f"🔎 [FactReview] review pass of account {account_id[:8]}: {len(task.items)} facts, "
                f"{len(task.questions)} questions, mirror={task.mirror}"
            )
            try:
                return await self._coordinator.route_message(message)
            except Exception as exc:
                logger.error(f"❌ [FactReview] the review pass of account {account_id[:8]} raised: {exc}", exc_info=True)
                return AgentResponse.failure(message.task_id, message.recipient, f"{type(exc).__name__}: {exc}")

    async def _task(
        self, user_id: str, account_id: str, batch: Sequence[FactEntity], questions: Sequence[FactReviewQuestion],
        now: datetime,
    ) -> FactReviewTask:
        """The prefetch: per fact its lineage and nearest current facts, the open questions about the batch, and in
        mirror mode the owner's recent messages, else (fixed language) the language the pass translates into, none when it
        cannot be read (the pass then translates nothing). `now` is the run's instant, which the pass then uses too."""
        items: List[ReviewItem] = []
        for fact in batch:
            lineage = await self._repo.get_lineage(account_id, fact.lineage_id)
            items.append(review_item(fact, lineage, await self._nearest(user_id, account_id, fact)))
        mirror = await self._texts.mirrors(user_id)
        language = None if mirror else await self._texts.pass_language(user_id)
        return FactReviewTask(
            items=tuple(items),
            questions=questions_about(batch, questions),
            mirror=mirror,
            owner_messages=await self._owner_messages(user_id) if mirror else (),
            now=now,
            language=language,
        )

    async def _owner_messages(self, user_id: str) -> Tuple[str, ...]:
        """The owner's recent messages in his channel's session (the one notifications go to), as the review reads them."""
        channel = await self._notifications.resolve_channel(user_id)
        if channel is None:
            return ()
        session = await self._session_store.load_session(session_id_for(user_id, channel.channel_id))
        return owner_messages_block(owner_message_texts(session.history))

    async def _offer(
        self, account_id: str, batch: Sequence[FactEntity], questions: Sequence[FactReviewQuestion], now: datetime,
    ) -> None:
        state = await self._review_store.get_state(account_id)
        if not await self._review_store.set_offers(account_id, offered(state.offers if state else {}, batch)):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; offers not counted")
        for question in questions:
            if question.reply_not_shown and not await self._review_store.mark_offered(question.question_id, now):
                logger.warning(f"[FactReview] question {question.question_id}: reply not marked shown (it changed)")

    # ------------------------------------------------------------------------------------------------ step 11

    async def _after_pass(
        self,
        user_id: str,
        account_id: str,
        batch: Sequence[FactEntity],
        response: Optional[AgentResponse],
        now: datetime,
        zone: str,
        writes: _FactWrites,
    ) -> _StepResult:
        """Step 11, over what `after_pass` decides: the decided facts' questions close `review` (`answer_service`,
        which releases the next when one was current); the undecided timed-out candidates get their fallback (`_fallbacks`,
        as in step 6); every ask is queued (`_queue`, as in step 7); then the `offers` entries of the decided and the queued facts go (`offers_removed`); the reported language and labels go to the state (`language_to_store`).
        The pass's quote translations are stored first (`_store_translations`, best effort), so a later failure of the
        step does not lose them. A queued ask then supersedes its folds (`_fold`), and each routine invoice of the batch
        gets its Cabinet question (`_invoices`). Each fact write is marked through `writes.wrote()` as it lands. None
        (nothing routed) does nothing."""
        if response is None:
            return _StepResult()
        result = decode_review_result(response.result)
        plan = after_pass(batch, await self._review_store.questions_for_account(account_id), result, now)
        await self._store_translations(account_id, result.translations, now)
        counts = dict(plan.counts)
        revived = list(plan.revived)
        for question in plan.closes:
            await self._answer_service.close_question(question, ClosedBy.REVIEW, source=review_close_source(question))
        settled = await self._fallbacks(account_id, [question for _, question in plan.fallbacks], now, zone, writes)
        for name, value in settled.counts.items():
            counts[name] = counts.get(name, 0) + value
        revived.extend(settled.revived)
        queued: List[str] = []
        for fact, ask in plan.asks:
            if await self._queue(account_id, fact.id, self._ask_builder(user_id, ask, now), writes):
                queued.append(fact.id)
                counts[CounterField.QUEUED] = counts.get(CounterField.QUEUED, 0) + 1
                folded = await self._fold(account_id, fact, ask, zone, writes)
                if folded:
                    counts[CounterField.FOLDED] = counts.get(CounterField.FOLDED, 0) + folded
        invoices = await self._invoices(user_id, account_id, batch, result.invoices, now)
        if pass_set_aside(counts):
            await self._note_set_aside(account_id, now)
        removed = plan.offers_removed(queued)
        if removed and not await self._review_store.mark_removed(account_id, removed):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; offers not cleared")
        await self._store_language(account_id, result.message_language, result.labels)
        logger.info(
            f"[FactReview] after the pass of account {account_id[:8]}: {len(plan.decided)} decided, "
            f"{len(plan.closes)} closed, {counts.get(CounterField.QUEUED, 0)}/{len(plan.asks)} queued, "
            f"{counts.get(CounterField.TIMED_OUT, 0)}/{len(plan.fallbacks)} fallbacks, "
            f"{counts.get(CounterField.FOLDED, 0)} folded, {invoices} invoices"
        )
        return _StepResult(counts=counts, revived=tuple(revived))

    async def _fallbacks(
        self, account_id: str, questions: Sequence[FactReviewQuestion], now: datetime, zone: str, writes: _FactWrites,
    ) -> _StepResult:
        """The fallback of each timed-out question (`_fallback`), counted as it lands: `timed_out`, `to_history` for a
        history fallback, `fallbacks_not_applied` for a history fallback that failed, and the facts it revived (the ones
        whose fallback neither sets the fact aside nor writes history) for the revive alert. The account is marked
        undigested when a fallback archived a fact or wrote history. Steps 6 and 11 both go through here."""
        timed_out = to_history = not_applied = 0
        noted = False
        revived: List[str] = []
        for question in questions:
            result = await self._fallback(account_id, question, now, zone, writes)
            not_applied += result.not_applied
            if result.written_id is None:
                continue
            timed_out += 1
            if result.wrote_history:
                to_history += 1
                noted = True
            elif fallback_sets_aside(question):
                noted = True
            else:
                revived.append(result.written_id)
        if noted:
            await self._note_set_aside(account_id, now)
        counts = {
            CounterField.TIMED_OUT: timed_out, CounterField.TO_HISTORY: to_history,
            CounterField.FALLBACKS_NOT_APPLIED: not_applied,
        }
        return _StepResult(counts={name: value for name, value in counts.items() if value}, revived=tuple(revived))

    async def _fallback(
        self, account_id: str, question: FactReviewQuestion, now: datetime, zone: str, writes: _FactWrites,
    ) -> "_Fallback":
        """A timed-out question's fallback on its fact as stored now, then the question closed `timeout`. `revive` and
        `archive`: `fallback_fields`, actor `timeout`, in place under the precondition. `history`: its `unknown` text
        written as a history rewrite through the typed path (`_history_fallback`). The written document's id; None when the
        fact moved on or the write conflicted (left for the next run, which sees the question again)."""
        stored = await self._asked_fact(account_id, question, "fallback not applied")
        if stored is None:
            return _Fallback()
        if fallback_writes_history(question):
            return await self._history_fallback(account_id, question, stored.fact, now, zone, writes)
        try:
            await self._repo.update_fact_fields(stored, fallback_fields(stored.fact, question, now, "timeout"))
        except FactWriteConflict:
            logger.warning(f"[FactReview] question {question.question_id}: the fallback's write conflicted; left")
            return _Fallback()
        writes.wrote()
        await self._answer_service.close_question(question, ClosedBy.TIMEOUT)
        return _Fallback(written_id=stored.fact.id)

    async def _history_fallback(
        self, account_id: str, question: FactReviewQuestion, fact: FactEntity, now: datetime, zone: str,
        writes: _FactWrites,
    ) -> "_Fallback":
        """The `history` fallback (C9): `history_fallback`'s update through `FactUpdatePort` under the actor `timeout`. Written:
        the question closes `timeout`. A conflict: left for the next run, nothing counted. Any other refusal, or an
        exception: `_history_failed`. Never raises."""
        change = history_fallback(fact, question, now, zone, "timeout")
        try:
            outcome = await self._fact_updates.apply_fact_update(
                fact.id, change.update, account_id, context=FactWriteContext.for_pass(PassActor.TIMEOUT, zone, None),
                evidence_at=change.evidence_at, reason=change.reason,
            )
        except Exception as exc:
            logger.warning(
                f"[FactReview] question {question.question_id}: the history fallback raised {type(exc).__name__}: {exc}"
            )
            return await self._history_failed(question, None)
        if outcome.refusal is UpdateRefusal.CONFLICT:
            logger.warning(f"[FactReview] question {question.question_id}: the history fallback's write conflicted; left")
            return _Fallback()
        if outcome.refusal is not None:
            logger.warning(
                f"[FactReview] question {question.question_id}: the history fallback was refused "
                f"({outcome.refusal.value}): {outcome.refusal_text}"
            )
            return await self._history_failed(question, outcome.refusal)
        writes.wrote()
        await self._answer_service.close_question(question, ClosedBy.TIMEOUT)
        return _Fallback(written_id=outcome.written_id, wrote_history=True)

    async def _history_failed(self, question: FactReviewQuestion, refusal: Optional[UpdateRefusal]) -> "_Fallback":
        """A history fallback that wrote nothing: the question closes `resolved_elsewhere` when a typed refusal comes at an
        attempt after the first (the spec stores no previous refusal) or at the third attempt, so it cannot loop; else the
        store counts the attempt and the question stays open for the next run."""
        attempts = question.fallback_attempts
        if (refusal is not None and attempts >= 1) or attempts + 1 >= _MAX_FALLBACK_ATTEMPTS:
            await self._answer_service.close_question(question, ClosedBy.RESOLVED_ELSEWHERE)
            logger.warning(f"[FactReview] question {question.question_id}: the history fallback failed {attempts + 1} times; closed")
        elif not await self._review_store.mark_fallback_failed(question.question_id):
            logger.warning(f"[FactReview] question {question.question_id}: the failed attempt was not counted (it changed)")
        return _Fallback(not_applied=1)

    async def _fold(
        self, account_id: str, fact: FactEntity, ask: AskRequest, zone: str, writes: _FactWrites,
    ) -> int:
        """The folds of a queued ask (P2-2): each stale fact the question absorbs is superseded by the asked fact. A refusal
        or an exception is a WARNING (the fold's id and the refusal, never a text) and the fold stays stale. How many landed."""
        landed = 0
        for fold_id in ask.folds:
            try:
                outcome = await self._fact_updates.apply_fact_update(
                    fold_id, ParsedFactUpdate(state=FactState.SUPERSEDED, superseded_by=fact.id), account_id,
                    context=FactWriteContext.for_pass(PassActor.REVIEW, zone, None), reason=superseded_reason(fact.id),
                )
            except Exception as exc:
                logger.warning(f"[FactReview] fold {fold_id[:8]} of fact {fact.id[:8]} raised {type(exc).__name__}: {exc}")
                continue
            if outcome.refusal is not None:
                logger.warning(
                    f"[FactReview] fold {fold_id[:8]} of fact {fact.id[:8]} stays stale: {outcome.refusal.value}"
                )
                continue
            writes.wrote()
            landed += 1
        return landed

    async def _invoices(
        self, user_id: str, account_id: str, batch: Sequence[FactEntity], requests: Sequence[InvoiceRequest],
        now: datetime,
    ) -> int:
        """A routine invoice the pass rewrote "payment not confirmed" (P2-2) gets one Cabinet-only question on the written
        version, never sent. A request for a fact outside the batch is ignored; a written version that is gone or not
        current is a WARNING and skipped. The account is marked undigested once when any was created. How many were."""
        in_batch = {fact.id for fact in batch}
        created = 0
        for request in requests:
            if request.fact_id not in in_batch:
                continue
            written = await self._repo.get_fact_by_id(request.written_id)
            if written is None or written.state is not FactState.CURRENT:
                logger.warning(f"[FactReview] invoice {request.written_id[:8]}: the written version is gone or not current; skipped")
                continue
            question = digest_question(written, uuid.uuid4().hex, user_id, request.paid, request.not_paid, at=now)
            if await self._review_store.create_queued(question):
                created += 1
        if created:
            await self._note_set_aside(account_id, now)
        return created

    @staticmethod
    def _ask_builder(user_id: str, ask: AskRequest, now: datetime) -> "_QueueBuild":
        """What an ask queues: the question with the pass's kind, texts and fallback (`queued_question`) and `queued_fields`.
        `fallback_date` is retired upstream (refused), so it is not passed."""

        def build(stored: FactEntity, question_id: str) -> Tuple[FactReviewQuestion, Dict[str, Any]]:
            question = queued_question(
                stored, question_id, user_id, ask.fallback,  # type: ignore[arg-type]  # validated by the question model
                at=now, noise=ask.noise, question=ask.question, texts=dict(ask.texts),  # type: ignore[arg-type]
            )
            return question, queued_fields(stored, question_id, now)

        return build

    async def _queue(self, account_id: str, fact_id: str, build: "_QueueBuild", writes: _FactWrites) -> bool:
        """One fact to the queue (an ask, step 11, or a kept-twice fact, step 7): `build` gives the `queued` record and
        the in-place write from the fact as read before that write; the fact is made current under its precondition,
        then the record is created. A fact that is not stale any more or a conflicting write leaves the fact as it is
        and creates no record (False)."""
        question_id = uuid.uuid4().hex
        try:
            stored = await self._repo.resolve_current(fact_id, account_id, QUEUE_ACCEPTS)
        except FactResolutionError as exc:
            logger.warning(f"[FactReview] fact {fact_id[:8]} not queued: the fact moved on ({exc})")
            return False
        if stored is None or stored.fact.id != fact_id:
            logger.warning(f"[FactReview] fact {fact_id[:8]} not queued: the fact moved on")
            return False
        question, fields = build(stored.fact, question_id)
        try:
            await self._repo.update_fact_fields(stored, fields)
        except FactWriteConflict:
            logger.warning(f"[FactReview] fact {fact_id[:8]} not queued: the fact's write conflicted")
            return False
        writes.wrote()
        if not await self._review_store.create_queued(question):
            logger.warning(f"[FactReview] question {question_id} for fact {fact_id[:8]} exists; not created again")
            return False
        return True

    async def _store_translations(
        self, account_id: str, translations: Sequence[QuoteTranslation], now: datetime,
    ) -> None:
        """Keep the pass's translations before anything else of the step can fail. Best effort: a failed write is a
        WARNING with the count and the error type (never a text); the owner then sees the stored text."""
        if not translations:
            return
        try:
            await self._review_store.set_quote_translations(account_id, translations, now)
        except Exception as exc:
            logger.warning(
                f"[FactReview] {len(translations)} quote translations of account {account_id[:8]} not stored: "
                f"{type(exc).__name__}"
            )

    async def _store_language(self, account_id: str, reported: Optional[str], labels: Any) -> None:
        state = await self._review_store.get_state(account_id)
        change = language_to_store(state, reported, labels)
        if change is None:
            return
        language, texts, digest = change
        if not await self._review_store.set_language(account_id, language, texts, digest):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; language {language} not kept")

    # ------------------------------------------------------------------------------------------------ step 15: alert

    async def _revive_alert(self, account_id: str, revived: Sequence[str], now: datetime) -> None:
        """One 🚨 naming the facts this run revived whose revives within the window reached the alert (`revive_alert_due`):
        their horizon is probably wrong. Best effort: a failed read or post is a WARNING."""
        if not revived:
            return
        try:
            due: List[Tuple[str, int]] = []
            for fact_id in revived:
                fact = await self._repo.get_fact_by_id(fact_id)
                if fact is not None and revive_alert_due(fact.lifecycle_events, now):
                    due.append((fact_id, revives_in_window(fact.lifecycle_events, now)))
            if due:
                await self._alert_sink.post(revive_alert(account_id, due))
        except Exception as exc:
            logger.warning(f"[FactReview] revive alert of account {account_id[:8]} not posted: {type(exc).__name__}: {exc}")
