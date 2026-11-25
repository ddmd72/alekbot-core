"""MemoryInboxFinalPass: the memory inbox's final pass over one user's `confirmed` candidates (MCP_MEMORY_SUBMISSIONS_RFC
§9, §10.3), the `FinalPass` the final runner calls under the account's memory-write lease.

The pass reads the user's `confirmed` candidates once and sends them oldest receipt first in batches of FINAL_BATCH, one
`consolidate_memory_inbox` agent task per batch, routed to the user's consolidation agent through the coordinator, inside
the user's RequestContext, with the lease's time left as its timeout. A candidate confirmed after the read is not in any
batch and waits for its own run (Review Focus 3).

After each batch, in a `finally` (so also when the agent timed out or this run is cancelled), `settle_final` marks the
batch: a pass that gave its final report with no raise marks every sent candidate `consolidated`, outcome `done`, whatever
the model did with it; any other end leaves them `confirmed` with one more attempt, and a candidate at FINAL_MAX_ATTEMPTS
becomes `consolidated`, outcome `gave_up`, with one 🚨 posted through the alert sink (ids and count, never text). Only a
candidate still `confirmed` is touched (the store's precondition).

A batch that did not end stops the run: the runner asks for one more run an hour later. The run is `completed` (every
batch ended), `empty` (nothing confirmed), `partial` (some batches ended, then one did not or the lease ran short) or
`failed` (none ended). A store failure propagates. Logs carry ids and counts, never text; the run's summary line is the
runner's.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Literal, Optional, Sequence, Tuple

from ..domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from ..domain.consolidation import FINAL_AGENT_TASK, consolidation_agent_id
from ..domain.memory_inbox import FINAL_BATCH, FINAL_MAX_ATTEMPTS, Candidate, CandidateStatus
from ..domain.memory_inbox_final_task import by_receipt, decode_final_result, encode_final_task
from ..domain.memory_inbox_runs import FinalOutcome, PassRequest
from ..domain.memory_write_lease import LEASE_MARGIN, agent_timeout_s
from ..domain.request_context import RequestContext
from ..ports.alert_sink import AlertSinkPort
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..utils.logger import logger

_SENDER = "memory_inbox_final_pass"
# A batch is not started with less of the lease left than this: it could not finish before the lease ends.
_MIN_PASS_S = LEASE_MARGIN.total_seconds()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gave_up_alert(user_id: str, candidate_ids: Sequence[str]) -> str:
    """The 🚨 line for candidates the final pass gave up on: the user, the count and the ids, never text."""
    return (f"🚨 [MemoryInbox] final pass gave up on {len(candidate_ids)} confirmed candidate(s) of user {user_id[:8]} "
            f"after {FINAL_MAX_ATTEMPTS} failed passes; they are marked consolidated and are not sent again: "
            f"{', '.join(candidate_ids)}")


class MemoryInboxFinalPass:
    """`coordinator` routes the agent task (`route_message`); `ensure_agents(user_id)` builds the user's agents before
    the first route; `alert_sink` (None: no alert) takes the gave-up 🚨."""

    def __init__(self, *, store: MemoryInboxRepository, coordinator: Any,
                 ensure_agents: Callable[[str], Awaitable[Any]], alert_sink: Optional[AlertSinkPort],
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._coordinator = coordinator
        self._ensure_agents = ensure_agents
        self._alert_sink = alert_sink
        self._clock = clock

    async def run(self, request: PassRequest) -> FinalOutcome:
        confirmed = by_receipt(await self._store.candidates(request.user_id, {CandidateStatus.CONFIRMED}))
        if not confirmed:
            return FinalOutcome(status="empty", candidates=0, wrote=0, gave_up=0)
        batches = [confirmed[i:i + FINAL_BATCH] for i in range(0, len(confirmed), FINAL_BATCH)]
        sent = wrote = gave_up = ended_batches = 0
        stopped = False
        for batch in batches:
            budget = agent_timeout_s(request.deadline, self._clock())
            if budget < _MIN_PASS_S:
                logger.warning(f"[MemoryInbox] final user={request.user_id[:8]} {budget:.0f} s of lease left; "
                               f"{len(confirmed) - sent} confirmed candidates wait for the next run")
                stopped = True
                break
            if sent == 0:
                await self._ensure_agents(request.user_id)
            ended, batch_wrote, batch_gave_up = await self._pass(request, batch, budget)
            sent += len(batch)
            wrote += batch_wrote
            gave_up += len(batch_gave_up)
            if batch_gave_up:
                await self._alert(request.user_id, batch_gave_up)
            if not ended:
                stopped = True
                break
            ended_batches += 1
        status: Literal["completed", "failed", "partial"] = \
            "completed" if not stopped else ("partial" if ended_batches else "failed")
        return FinalOutcome(status=status, candidates=sent, wrote=wrote, gave_up=gave_up)

    async def _pass(self, request: PassRequest, batch: List[Candidate],
                    budget: float) -> Tuple[bool, int, Tuple[str, ...]]:
        """One agent task over `batch`: (ended, writes, ids given up). The batch is settled in a `finally`, whatever
        the route did (also when this run is cancelled)."""
        ids = [c.candidate_id for c in batch]
        message = AgentMessage(
            task_id=uuid.uuid4().hex,
            sender=_SENDER,
            recipient=consolidation_agent_id(request.user_id),
            intent=AgentIntent.DELEGATE,
            payload={"task": FINAL_AGENT_TASK, **encode_final_task(batch)},
            context={"user_id": request.user_id, "account_id": request.account_id, "owner_timezone": request.zone,
                     "suppress_transient_retry": True},
            timeout_ms=int(budget * 1000),
        )
        ended, batch_wrote = False, 0
        try:
            ended, batch_wrote = self._read(request, len(batch), await self._route(request, message))
        finally:
            settled = await self._store.settle_final(ids, ended, self._clock())
            if settled.gave_up:
                logger.error(f"❌ [MemoryInbox] final user={request.user_id[:8]} gave up on "
                             f"{len(settled.gave_up)} candidates: {', '.join(settled.gave_up)}")
        return ended, batch_wrote, settled.gave_up

    @staticmethod
    def _read(request: PassRequest, sent: int, response: Optional[AgentResponse]) -> Tuple[bool, int]:
        """(ended, writes) of the agent's answer; (False, 0) for a pass that died or answered an unreadable result."""
        if response is None or response.status is not AgentStatus.SUCCESS:
            error = None if response is None else f"{response.status.value}: {response.error}"
            logger.error(f"❌ [MemoryInbox] final pass of user {request.user_id[:8]} failed ({error}); its {sent} "
                         f"candidates stay confirmed with one more attempt")
            return False, 0
        try:
            ended, wrote = decode_final_result(response.result)
        except ValueError as exc:
            logger.error(f"❌ [MemoryInbox] final pass of user {request.user_id[:8]} answered an unreadable result: "
                         f"{exc}; its {sent} candidates stay confirmed with one more attempt")
            return False, 0
        if not ended:
            logger.warning(f"⚠️ [MemoryInbox] final pass of user {request.user_id[:8]} raised or used all its turns "
                           f"after {wrote} writes; its {sent} candidates stay confirmed with one more attempt")
        return ended, wrote

    async def _route(self, request: PassRequest, message: AgentMessage) -> Optional[AgentResponse]:
        try:
            async with RequestContext(user_id=request.user_id, account_id=request.account_id):
                response: AgentResponse = await self._coordinator.route_message(message)
                return response
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] final pass of user {request.user_id[:8]} raised: {type(exc).__name__}: "
                         f"{exc}", exc_info=True)
            return None

    async def _alert(self, user_id: str, candidate_ids: Sequence[str]) -> None:
        """One 🚨 when a sink is configured; a sink failure is logged, never raised."""
        if self._alert_sink is None:
            return
        try:
            await self._alert_sink.post(gave_up_alert(user_id, candidate_ids))
        except Exception as exc:
            logger.warning(f"[MemoryInbox] the gave-up alert failed: {type(exc).__name__}: {exc}")
