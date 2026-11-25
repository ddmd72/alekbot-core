"""MemoryInboxTriagePass: the memory inbox's triage run over one user's `received` submissions (MCP_MEMORY_SUBMISSIONS_RFC
§7, §10.2), the `TriagePass` the rhythm calls under the account's memory-write lease.

A run is up to TRIAGE_PASSES passes. Each pass reads up to TRIAGE_BATCH `received` submissions oldest first (leaving out
those an earlier pass of this run was already sent, so an undecided one is not offered twice in one run), every open
candidate and the newest TRIAGE_SHOWN_CAP answered ones; it builds the pass's triage scope (`new_scope`) and routes one
`triage_memory_inbox` task, the scope in its payload (`TRIAGE_SCOPE_KEY`), to the user's consolidation agent through the
coordinator, inside the user's RequestContext, with the lease's time left as its timeout. What is left waits for the
next run.

The service owns the scope, so the decisions it holds are written whatever happened to the agent (§7.4: decisions
recorded before a turn cap or a failure stand): after the route, in a `finally`, `apply_triage` writes the scope's
decisions over the submissions the pass sent, and only those with no decision get one more offer. A pass that died (the
coordinator raised, the agent answered a failure, a timeout or an unreadable result) or whose loop raised or used all
its turns ends the run `failed`; a poison batch, never decided, ends `discarded` `undecided` after TRIAGE_MAX_OFFERS runs
instead of looping (Review Focus 2). A store failure propagates. Logs carry ids and counts, never text.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Set

from ..domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from ..domain.consolidation import TRIAGE_AGENT_TASK, consolidation_agent_id
from ..domain.memory_inbox import (
    TRIAGE_BATCH,
    TRIAGE_PASSES,
    TRIAGE_SHOWN_CAP,
    AppliedTriage,
    CandidateStatus,
    Submission,
    TriageDecisions,
)
from ..domain.memory_inbox_runs import PassRequest, TriageOutcome
from ..domain.memory_inbox_triage_task import (
    TRIAGE_SCOPE_KEY,
    TriageScope,
    TriageTask,
    decode_triage_result,
    encode_triage_task,
)
from ..domain.memory_write_lease import LEASE_MARGIN, agent_timeout_s
from ..domain.request_context import RequestContext
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..utils.logger import logger

_SENDER = "memory_inbox_triage_pass"
# A pass is not started with less of the lease left than this: it could not finish before the lease ends.
_MIN_PASS_S = LEASE_MARGIN.total_seconds()
# The `left` count reads at most this many waiting submissions (a log count; the daily cap keeps it far lower).
_LEFT_READ_CAP = 500

# Builds one pass's triage scope over the submission ids it is sent, for the account (composition wires the concrete
# `MemoryInboxTriageScope` over the fact repository).
ScopeFactory = Callable[[Sequence[str], str], TriageScope]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class _PassResult:
    decisions: TriageDecisions
    applied: AppliedTriage
    ok: bool


class MemoryInboxTriagePass:
    """`coordinator` routes the agent task (`route_message`); `ensure_agents(user_id)` builds the user's agents before
    the first route; `new_scope` builds each pass's triage scope."""

    def __init__(self, *, store: MemoryInboxRepository, coordinator: Any,
                 ensure_agents: Callable[[str], Awaitable[Any]], new_scope: ScopeFactory,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._coordinator = coordinator
        self._ensure_agents = ensure_agents
        self._new_scope = new_scope
        self._clock = clock

    async def run(self, request: PassRequest) -> TriageOutcome:
        sent: Set[str] = set()
        sources: Dict[str, None] = {}
        submissions = candidates = discarded = passes = 0
        status: Literal["completed", "empty", "failed"] = "completed"
        for _ in range(TRIAGE_PASSES):
            batch = [s for s in await self._store.received(request.user_id, TRIAGE_BATCH + len(sent))
                     if s.submission_id not in sent][:TRIAGE_BATCH]
            if not batch:
                break
            budget = agent_timeout_s(request.deadline, self._clock())
            if budget < _MIN_PASS_S:
                logger.warning(f"[MemoryInbox] triage user={request.user_id[:8]} {budget:.0f} s of lease left; "
                               f"the rest waits for the next run")
                status = status if passes else "failed"
                break
            if passes == 0:
                await self._ensure_agents(request.user_id)
            result = await self._pass(request, batch, budget)
            passes += 1
            sent.update(s.submission_id for s in batch)
            submissions += len(batch)
            candidates += result.applied.candidates
            discarded += result.applied.discarded + result.applied.undecided
            for name in _cited_clients(batch, result.decisions):
                sources.setdefault(name)
            if not result.ok:
                status = "failed"
                break
            if len(batch) < TRIAGE_BATCH:
                break
        if passes == 0 and status == "completed":
            status = "empty"
        left = 0 if status == "empty" else len(await self._store.received(request.user_id, _LEFT_READ_CAP))
        logger.info(f"[MemoryInbox] triage user={request.user_id[:8]} account={request.account_id[:8]} "
                    f"submissions={submissions} candidates={candidates} discarded={discarded} left={left} "
                    f"passes={passes} status={status}")
        return TriageOutcome(status=status, submissions=submissions, candidates=candidates,
                             discarded=discarded, left=left, passes=passes, sources=tuple(sources))

    async def _pass(self, request: PassRequest, batch: List[Submission], budget: float) -> _PassResult:
        """One agent task over `batch`. The scope's decisions are written in a `finally`, whatever the route did (also
        when this run is cancelled); `ok` is whether the pass ended well (the model's final report, no raise)."""
        ids = [s.submission_id for s in batch]
        scope = self._new_scope(ids, request.account_id)
        task = TriageTask(
            submissions=tuple(batch),
            waiting=tuple(await self._store.candidates(request.user_id,
                                                       {CandidateStatus.CANDIDATE, CandidateStatus.CONFIRMED})),
            answered=tuple(await self._store.candidates(request.user_id,
                                                        {CandidateStatus.REJECTED, CandidateStatus.CONSOLIDATED},
                                                        limit=TRIAGE_SHOWN_CAP)),
            language=request.language,
        )
        message = AgentMessage(
            task_id=uuid.uuid4().hex,
            sender=_SENDER,
            recipient=consolidation_agent_id(request.user_id),
            intent=AgentIntent.DELEGATE,
            payload={"task": TRIAGE_AGENT_TASK, **encode_triage_task(task), TRIAGE_SCOPE_KEY: scope},
            context={"user_id": request.user_id, "account_id": request.account_id, "owner_timezone": request.zone,
                     "suppress_transient_retry": True},
            timeout_ms=int(budget * 1000),
        )
        ok = False
        try:
            ok = self._ended(request, len(batch), await self._route(request, message))
        finally:
            decisions = scope.decisions()
            applied = await self._store.apply_triage(request.user_id, request.account_id, ids, decisions,
                                                     request.language, self._clock())
        return _PassResult(decisions=decisions, applied=applied, ok=ok)

    @staticmethod
    def _ended(request: PassRequest, sent: int, response: Optional[AgentResponse]) -> bool:
        """Whether the agent's answer says the pass ended with the model's final report and no raise."""
        if response is None or response.status is not AgentStatus.SUCCESS:
            error = None if response is None else f"{response.status.value}: {response.error}"
            logger.error(f"❌ [MemoryInbox] triage pass of user {request.user_id[:8]} failed ({error}); the decisions "
                         f"it recorded stand, the other of its {sent} submissions are offered once more")
            return False
        try:
            _, ended, failed = decode_triage_result(response.result)
        except ValueError as exc:
            logger.error(f"❌ [MemoryInbox] triage pass of user {request.user_id[:8]} answered an unreadable result: "
                         f"{exc}; the decisions it recorded stand")
            return False
        if failed or not ended:
            logger.warning(f"⚠️ [MemoryInbox] triage pass of user {request.user_id[:8]} "
                           f"{'raised' if failed else 'used all its turns'}; its decisions stand")
        return ended and not failed

    async def _route(self, request: PassRequest, message: AgentMessage) -> Optional[AgentResponse]:
        try:
            async with RequestContext(user_id=request.user_id, account_id=request.account_id):
                response: AgentResponse = await self._coordinator.route_message(message)
                return response
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] triage pass of user {request.user_id[:8]} raised: {type(exc).__name__}: "
                         f"{exc}", exc_info=True)
            return None


def _cited_clients(batch: List[Submission], decisions: TriageDecisions) -> List[str]:
    """The client names of the batch submissions the decided candidates cite, in candidate order."""
    by_id = {s.submission_id: s.client_name for s in batch}
    return [by_id[i] for c in decisions.candidates for i in c.source_ids if i in by_id]
