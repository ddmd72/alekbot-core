"""FactReviewService — one memory-review run for an account (Facts S4 plan §3.2, §3.7).

`run(user_id, account_id, mode)` is the whole run: the owner check, then, for `daily`, the setup, the account's
memory-write lease (the one `ConsolidationService` holds, so a review and a consolidation never write at once),
housekeeping of questions whose send is over, the review pass, the send of the next question, the biographical cache,
the digest, the counters and the state marks. `next` only sends the next question (no lease, no model).

The clock is read once per run; that `now` is every step's and every domain decision's. The one other read is the lease
budget at routing time (as `ConsolidationService`): the agent's timeout is what is left of the lease then, so the
pass ends before the lease does however long the steps before it took. Every question close goes through
`FactAnswerService.close_question`; nothing here closes one itself.

Built: steps 1-5 and 8-15 and the failure rule (T8a, T8b). The timeouts and kept-twice questions (T8c), the send (T8d1)
and the digest (T8d2) are stubs that return an empty result.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from ..domain.entities import FactEntity
from ..domain.exceptions import FactResolutionError, FactWriteConflict
from ..domain.fact_lifecycle import REVIEW_ACCEPTS
from ..domain.fact_lifecycle_day import CounterField
from ..domain.fact_review_after_pass import after_pass
from ..domain.fact_review_ask import AskRequest
from ..domain.fact_review_frame import NOT_DELIVERED_REPLY, not_delivered_frame
from ..domain.fact_review_language import language_to_store
from ..domain.fact_review_question import ClosedBy, FactReviewQuestion
from ..domain.fact_review_result import decode_review_result
from ..domain.fact_review_rules import (
    QUEUE_ACCEPTS,
    becomes_unanswered,
    fallback_fields,
    fallback_revives,
    offered,
    queued_fields,
    queued_question,
    revive_alert_due,
    revives_in_30_days,
    review_close_source,
    select_batch,
    stuck_pending,
    unread_replies,
)
from ..domain.fact_review_state import FactReviewState
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
    REVIEW_MODES,
    ReviewMode,
    defer_key,
    review_payload,
)
from ..domain.llm import Message, MessagePart
from ..domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, LEASE_MARGIN, agent_timeout_s, lease_until
from ..domain.owner_timezone import not_owner_reason
from ..domain.request_context import RequestContext
from ..ports.account_repository import AccountRepository
from ..ports.alert_sink import AlertSinkPort
from ..ports.channel_binding_port import ChannelBindingPort
from ..ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from ..ports.fact_review_store import FactReviewStore
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

_DEFERRAL_DEADLINE_S = 1800  # the Cloud Tasks dispatch deadline of a deferred run, as ConsolidationService's
_MARGIN_S = LEASE_MARGIN.total_seconds()
_SENDER = "fact_review_service"
# The consolidation agent's task name (`Intent.REVIEW_STALE_FACTS`; a literal, as ConsolidationService's own task names,
# because services do not import infrastructure).
_REVIEW_TASK = "review_stale_facts"


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
    - `agent_factory`, `owner_zones`, `texts`: the setup (step 3); `texts` also says whether the language mirrors the
      owner (step 10), gives the send its message (T8d1) and the digest its lines (T8d2);
    - `lease`, `task_queue`, `run_timeout_s`: the lease and the deferral (step 4, and step 10 when no lease time is
      left); `task_queue` also takes the next question's release through `answer_service`;
    - `review_store`, `session_store`: housekeeping (step 5) and every question and state write; `session_store` also
      gives the owner's recent messages (step 10, mirror mode);
    - `repo`: the stale facts, the prefetch and the service's fact writes (steps 8, 10, 11; T8c), the biographical
      cache (step 13), the revive alert's read (step 15);
    - `answer_service`: every question close (step 11; T8c);
    - `coordinator`: the review pass (step 10);
    - `notification_state`, `channel_bindings`, `interactive`: the send's channel and post (T8d1);
    - `notifications`: the owner's channel for his recent messages (step 10), the digest (T8d2);
    - `alert_sink`: the revive alert (step 15);
    - `counters`: the day's counters (step 15);
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
        language = await self._texts.language_for(user_id, account_id)
        owner = uuid.uuid4().hex
        until = lease_until(now, self._run_timeout_s)
        if not await self._lease.acquire(account_id, owner, now, until):
            await self._defer(user_id, account_id, now)
            return {"status": "deferred"}
        try:
            await self._mark("started", self._review_store.mark_started, account_id, now)
            return await self._leased(user_id, account_id, now, zone, language, until)
        finally:
            await self._release(account_id, owner)

    async def _defer(self, user_id: str, account_id: str, now: datetime) -> None:
        await self._task_queue.enqueue_worker_task(
            FACT_REVIEW_TASK,
            review_payload(user_id, account_id, "daily"),
            delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
            deadline_seconds=_DEFERRAL_DEADLINE_S,
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
        self, user_id: str, account_id: str, now: datetime, zone: str, language: str, until: datetime,
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
            batch = await self._select(account_id, state, questions, timeouts.candidates, now)
            failed = deferred = False
            if batch:  # step 9: nothing selected, no model call
                response = await self._route(user_id, account_id, batch, questions, now, zone, language, until)
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
            if question.retraction_due:
                await self._append_pair(question, not_delivered_frame(question.question_id), NOT_DELIVERED_REPLY)
            if await self._review_store.requeue_front(question.question_id, failed_today=False):
                logger.info(f"[FactReview] question {question.question_id} was never posted: back at the queue's front")
                return question.requeued()
        logger.warning(f"[FactReview] stuck question {question.question_id} not settled: it changed meanwhile")
        return question

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
            await self._counters.add(account_id, now.astimezone(timezone.utc).date(), counts)
        except Exception as exc:
            logger.warning(
                f"[FactReview] {', '.join(c.value for c in counts)} not counted for account {account_id[:8]}: "
                f"{type(exc).__name__}"
            )

    # ------------------------------------------------------------------------------------------------ stubs (T8c-T8d2)

    async def _timeouts(
        self,
        user_id: str,
        account_id: str,
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
        writes: _FactWrites,
    ) -> _Timeouts:
        """T8c, step 6: the timed-out questions; a candidate joins the batch, else the fallback or `resolved_elsewhere`.
        Mark every fact write at once through `writes.wrote()`, right after it lands."""
        return _Timeouts()

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
        """T8c, step 7: a stale fact offered twice gets a code question and is queued.
        Mark every fact write at once through `writes.wrote()`, right after it lands."""
        return _StepResult()

    async def _send_next(
        self, user_id: str, account_id: str, now: datetime, zone: str, writes: _FactWrites,
    ) -> _StepResult:
        """T8d1, step 12 (§3.7): claim, pair, post, asked; not delivered → the second pair and the queue's front.
        Mark every fact write at once through `writes.wrote()`, right after it lands."""
        return _StepResult()

    async def _digest(self, user_id: str, account_id: str, now: datetime, zone: str) -> None:
        """T8d2, step 14 (§3.11): the weekly digest when due."""
        return None

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
        the question's timeout then settles it (T8c)."""
        try:
            stored = await self._repo.resolve_current(question.fact_id, account_id, REVIEW_ACCEPTS)
        except FactResolutionError as exc:
            stored, why = None, str(exc)
        else:
            why = "it is gone or changed"
        if stored is None or stored.fact.id != question.fact_id:
            logger.info(f"[FactReview] question {question.question_id}: its fact is not reviewed with the reply ({why})")
            return None
        return stored.fact

    # ------------------------------------------------------------------------------------------------ step 10: route

    async def _route(
        self,
        user_id: str,
        account_id: str,
        batch: Sequence[FactEntity],
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
        language: str,
        until: datetime,
    ) -> Optional[AgentResponse]:
        """Step 10: the prefetch (§3.4), then the lease budget read now; less than the margin left defers the run (as
        `ConsolidationService`) and routes nothing (None). Else `offers` (+1 per stale batch fact) and `offered_at` (each
        reply shown) are written and the agent task is routed inside `RequestContext`, its timeout the budget. A
        coordinator exception is the pass's failure (logged, a failed response): the failure rule, not a 5xx. `language`
        is not read here: the pass is told whether the language mirrors the owner, and the agent words its request."""
        async with RequestContext(user_id=user_id, account_id=account_id):
            task = await self._task(user_id, account_id, batch, questions)
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
                recipient=f"consolidation_agent_{user_id}",
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
    ) -> FactReviewTask:
        """The prefetch: per fact its lineage and nearest current facts, the open questions about the batch, and in
        mirror mode the owner's recent messages."""
        items: List[ReviewItem] = []
        for fact in batch:
            lineage = await self._repo.get_lineage(account_id, fact.lineage_id)
            nearest: List[FactEntity] = []
            if fact.vector:
                nearest = await self._repo.search_facts(
                    list(fact.vector), limit=NEIGHBOUR_QUERY, user_id=user_id, account_id=account_id,
                )
            items.append(review_item(fact, lineage, nearest))
        mirror = await self._texts.mirrors(user_id)
        return FactReviewTask(
            items=tuple(items),
            questions=questions_about(batch, questions),
            mirror=mirror,
            owner_messages=await self._owner_messages(user_id) if mirror else (),
        )

    async def _owner_messages(self, user_id: str) -> Tuple[str, ...]:
        """The owner's recent messages in his channel's session (the one notifications go to), as the review reads them."""
        channel = await self._notifications.resolve_channel(user_id)
        if channel is None:
            return ()
        session = await self._session_store.load_session(f"{user_id}:{channel.channel_id}")
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
        """Step 11, over what `after_pass` decides: the decided facts' `offers` entries go; their questions close
        `review` (`answer_service`, which releases the next when one was current); the undecided timed-out candidates
        get their fallback; every ask is queued; the reported language and labels go to the state (`language_to_store`).
        Each fact write is marked through `writes.wrote()` as it lands. None (nothing routed) does nothing."""
        if response is None:
            return _StepResult()
        result = decode_review_result(response.result)
        plan = after_pass(batch, await self._review_store.questions_for_account(account_id), result, now)
        counts = dict(plan.counts)
        revived = list(plan.revived)
        if plan.decided and not await self._review_store.mark_removed(account_id, plan.decided):
            logger.warning(f"[FactReview] account {account_id[:8]} has no review state; offers not cleared")
        for question in plan.closes:
            await self._answer_service.close_question(question, ClosedBy.REVIEW, source=review_close_source(question))
        for _, question in plan.fallbacks:
            written = await self._fallback(account_id, question, now, writes)
            if written is not None:
                counts[CounterField.TIMED_OUT] = counts.get(CounterField.TIMED_OUT, 0) + 1
                if fallback_revives(question):
                    revived.append(written)
        for fact, ask in plan.asks:
            if await self._queue(user_id, account_id, fact, ask, now, writes):
                counts[CounterField.QUEUED] = counts.get(CounterField.QUEUED, 0) + 1
        await self._store_language(account_id, result.message_language, result.labels)
        logger.info(
            f"[FactReview] after the pass of account {account_id[:8]}: {len(plan.decided)} decided, "
            f"{len(plan.closes)} closed, {counts.get(CounterField.QUEUED, 0)}/{len(plan.asks)} queued, "
            f"{counts.get(CounterField.TIMED_OUT, 0)}/{len(plan.fallbacks)} fallbacks"
        )
        return _StepResult(counts=counts, revived=tuple(revived))

    async def _fallback(
        self, account_id: str, question: FactReviewQuestion, now: datetime, writes: _FactWrites,
    ) -> Optional[str]:
        """A timed-out question's fallback (`fallback_fields`, actor `timeout`) on its fact as stored now, under the
        precondition, then the question closed `timeout`. The written document's id; None when the fact moved on or the
        write conflicted (left for the next run, which sees the question again)."""
        try:
            stored = await self._repo.resolve_current(question.fact_id, account_id, REVIEW_ACCEPTS)
        except FactResolutionError as exc:
            logger.info(f"[FactReview] question {question.question_id}: fallback not applied, its fact moved on: {exc}")
            return None
        if stored is None or stored.fact.id != question.fact_id:
            logger.info(f"[FactReview] question {question.question_id}: fallback not applied, its fact moved on")
            return None
        try:
            await self._repo.update_fact_fields(stored, fallback_fields(stored.fact, question, now, "timeout"))
        except FactWriteConflict:
            logger.warning(f"[FactReview] question {question.question_id}: the fallback's write conflicted; left")
            return None
        writes.wrote()
        await self._answer_service.close_question(question, ClosedBy.TIMEOUT)
        return stored.fact.id

    async def _queue(
        self, user_id: str, account_id: str, fact: FactEntity, ask: AskRequest, now: datetime, writes: _FactWrites,
    ) -> bool:
        """One ask to the queue: the fact made current under its precondition (`queued_fields`), then the `queued`
        record built by `queued_question` from the fact as read before that write. A fact that is not stale any more or
        a conflicting write leaves the fact as it is and creates no record (False)."""
        question_id = uuid.uuid4().hex
        try:
            stored = await self._repo.resolve_current(fact.id, account_id, QUEUE_ACCEPTS)
        except FactResolutionError as exc:
            logger.warning(f"[FactReview] ask about fact {fact.id[:8]} not queued: the fact moved on ({exc})")
            return False
        if stored is None or stored.fact.id != fact.id:
            logger.warning(f"[FactReview] ask about fact {fact.id[:8]} not queued: the fact moved on")
            return False
        question = queued_question(
            stored.fact, question_id, user_id, ask.fallback,  # type: ignore[arg-type]  # validated by the question model
            at=now, fallback_date=ask.fallback_date, noise=ask.noise,
        )
        try:
            await self._repo.update_fact_fields(stored, queued_fields(stored.fact, question_id, now))
        except FactWriteConflict:
            logger.warning(f"[FactReview] ask about fact {fact.id[:8]} not queued: the fact's write conflicted")
            return False
        writes.wrote()
        if not await self._review_store.create_queued(question):
            logger.warning(f"[FactReview] question {question_id} for fact {fact.id[:8]} exists; not created again")
            return False
        return True

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
        """One 🚨 naming the facts this run revived whose revives within 30 days reached the alert (`revive_alert_due`):
        their horizon is probably wrong. Best effort: a failed read or post is a WARNING."""
        if not revived:
            return
        try:
            due: List[str] = []
            for fact_id in revived:
                fact = await self._repo.get_fact_by_id(fact_id)
                if fact is not None and revive_alert_due(fact.lifecycle_events, now):
                    due.append(f"{fact_id[:8]} ({revives_in_30_days(fact.lifecycle_events, now)}×)")
            if due:
                await self._alert_sink.post(
                    f"🚨 [FactReview] account {account_id[:8]}: revived again within 30 days, the horizon is probably "
                    f"wrong: {', '.join(due)}"
                )
        except Exception as exc:
            logger.warning(f"[FactReview] revive alert of account {account_id[:8]} not posted: {type(exc).__name__}: {exc}")
