"""FactReviewService — one memory-review run for an account (Facts S4 plan §3.2, §3.7).

`run(user_id, account_id, mode)` is the whole run: the owner check, then, for `daily`, the setup, the account's
memory-write lease (the one `ConsolidationService` holds, so a review and a consolidation never write at once),
housekeeping of questions whose send is over, the review pass, the send of the next question, the biographical cache,
the digest, the counters and the state marks. `next` only sends the next question (no lease, no model).

The clock is read once per run; that `now` is every step's. Every question close goes through
`FactAnswerService.close_question`; nothing here closes one itself.

Built here (T8a): steps 1-5, 9, 13 and 15, and the failure rule. The timeouts and kept-twice questions (T8c), selection,
routing and the after-pass writes (T8b), the send (T8d1) and the digest (T8d2) are stubs that return an empty result.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from ..domain.agent import AgentResponse, AgentStatus
from ..domain.entities import FactEntity
from ..domain.fact_lifecycle_day import CounterField
from ..domain.fact_review_frame import NOT_DELIVERED_REPLY, not_delivered_frame
from ..domain.fact_review_question import FactReviewQuestion
from ..domain.fact_review_rules import becomes_unanswered, stuck_pending
from ..domain.fact_review_state import FactReviewState
from ..domain.fact_review_task_keys import (
    FACT_REVIEW_TASK,
    REVIEW_MODES,
    ReviewMode,
    defer_key,
    review_payload,
)
from ..domain.llm import Message, MessagePart
from ..domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, LEASE_MARGIN, lease_until
from ..domain.owner_timezone import not_owner_reason
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _StepResult:
    """What one step did that the run must know: whether it wrote a fact (the biographical cache is refreshed once
    for every write of this service; the agent refreshes after its own) and what it counts."""

    wrote_facts: bool = False
    counts: Dict[CounterField, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _Timeouts(_StepResult):
    """Step 6: besides the writes, the timed-out questions' facts that join this run's batch."""

    candidates: Tuple[FactEntity, ...] = ()


@dataclass
class _Tally:
    """The run's own account of its steps."""

    wrote_facts: bool = False
    refreshed: bool = False
    counts: Dict[CounterField, int] = field(default_factory=dict)

    def add(self, step: _StepResult) -> None:
        self.wrote_facts = self.wrote_facts or step.wrote_facts
        for name, value in step.counts.items():
            self.counts[name] = self.counts.get(name, 0) + value

    def count(self, name: CounterField, value: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + value


class FactReviewService:
    """`run(user_id, account_id, mode="daily") -> {"status": ...}`; ports only.

    Dependencies, by the step that reads them:
    - `account_repo`, `user_repo`: the owner check (step 1);
    - `agent_factory`, `owner_zones`, `texts`: the setup (step 3); `texts` also gives the send its message (T8d1) and
      the digest its lines (T8d2);
    - `lease`, `task_queue`, `run_timeout_s`: the lease and the deferral (step 4); `task_queue` also takes the next
      question's release through `answer_service`;
    - `review_store`, `session_store`: housekeeping (step 5) and every question and state write;
    - `repo`: the stale facts and their writes (T8b, T8c), the biographical cache (step 13);
    - `answer_service`: every question close (T8b, T8c);
    - `coordinator`: the review pass (T8b, step 10);
    - `notification_state`, `channel_bindings`, `interactive`: the send's channel and post (T8d1);
    - `notifications`: the digest (T8d2);
    - `alert_sink`: the revive alert (T8b, step 15);
    - `counters`: the day's counters (step 15);
    - `clock`: read once per run.
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
        lease is held (one named task per account and bucket); else `completed`, or `failed` when the review pass failed
        (`review_failed`, `mark_failed`; the send and the digest still run and every write stands). A lease, store or
        queue error raises. Raises ValueError for a mode that is neither."""
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
        tally.add(await self._send_next(user_id, account_id, now, zone))
        await self._count(account_id, now, tally)
        return {"status": "completed"}

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
        try:
            await self._housekeeping(account_id, now, zone)
            state = await self._review_store.get_state(account_id)
            questions = await self._review_store.questions_for_account(account_id)
            timeouts = await self._timeouts(user_id, account_id, questions, now, zone)
            tally.add(timeouts)
            tally.add(await self._kept_twice(user_id, account_id, state, questions, now, zone))
            batch = await self._select(account_id, state, questions, timeouts.candidates, now)
            failed = False
            if batch:  # step 9: nothing selected, no model call
                response = await self._route(user_id, account_id, batch, questions, now, zone, language, until)
                if response is not None and response.status is not AgentStatus.SUCCESS:
                    failed = True
                    logger.warning(
                        f"[FactReview] the review pass failed for account {account_id[:8]}: {response.error}"
                    )
                else:
                    tally.add(await self._after_pass(user_id, account_id, batch, response, now, zone))
            tally.add(await self._send_next(user_id, account_id, now, zone))  # step 12
            await self._refresh_once(account_id, tally)  # step 13
            await self._digest(user_id, account_id, now, zone)  # step 14
            if failed:
                tally.count(CounterField.REVIEW_FAILED)
            await self._count(account_id, now, tally)  # step 15
            if failed:
                await self._mark("failed", self._review_store.mark_failed, account_id, now)
                return {"status": "failed"}
            await self._mark("succeeded", self._review_store.mark_succeeded, account_id, now)
            return {"status": "completed"}
        finally:
            if tally.wrote_facts and not tally.refreshed:
                try:
                    await self._refresh_once(account_id, tally)
                except Exception as exc:
                    logger.error(
                        f"[FactReview] biographical cache of account {account_id[:8]} not refreshed after the review's "
                        f"writes: {type(exc).__name__}: {exc}"
                    )

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

    async def _refresh_once(self, account_id: str, tally: _Tally) -> None:
        """Step 13: once, when this service wrote a fact."""
        if tally.wrote_facts and not tally.refreshed:
            tally.refreshed = True
            await self._repo.refresh_biographical_context_cache(owner_id=account_id)

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

    # ------------------------------------------------------------------------------------------------ stubs (T8b-T8d2)

    async def _timeouts(
        self, user_id: str, account_id: str, questions: Sequence[FactReviewQuestion], now: datetime, zone: str,
    ) -> _Timeouts:
        """T8c, step 6: the timed-out questions; a candidate joins the batch, else the fallback or `resolved_elsewhere`."""
        return _Timeouts()

    async def _kept_twice(
        self,
        user_id: str,
        account_id: str,
        state: Optional[FactReviewState],
        questions: Sequence[FactReviewQuestion],
        now: datetime,
        zone: str,
    ) -> _StepResult:
        """T8c, step 7: a stale fact offered twice gets a code question and is queued."""
        return _StepResult()

    async def _select(
        self,
        account_id: str,
        state: Optional[FactReviewState],
        questions: Sequence[FactReviewQuestion],
        candidates: Sequence[FactEntity],
        now: datetime,
    ) -> List[FactEntity]:
        """T8b, step 8: the batch (`select_batch`)."""
        return []

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
        """T8b, step 10: prefetch, `offers` and `offered_at`, and the agent task inside `RequestContext`; the agent's
        response (a failed one is the run's failure), None when nothing was routed."""
        return None

    async def _after_pass(
        self,
        user_id: str,
        account_id: str,
        batch: Sequence[FactEntity],
        response: Optional[AgentResponse],
        now: datetime,
        zone: str,
    ) -> _StepResult:
        """T8b, step 11: outcomes, review closes, the timed-out candidates' fallback, asks to the queue, language."""
        return _StepResult()

    async def _send_next(self, user_id: str, account_id: str, now: datetime, zone: str) -> _StepResult:
        """T8d1, step 12 (§3.7): claim, pair, post, asked; not delivered → the second pair and the queue's front."""
        return _StepResult()

    async def _digest(self, user_id: str, account_id: str, now: datetime, zone: str) -> None:
        """T8d2, step 14 (§3.11): the weekly digest when due."""
        return None
