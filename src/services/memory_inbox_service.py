"""MemoryInboxService: the memory inbox's entry points for the MCP tool and the Cabinet (MCP_MEMORY_SUBMISSIONS_RFC §5.6,
§8.2, §10.3).

- `submit`: an outside assistant's normalised items, stored as `received` under the owner-local day's cap (the day is
  the account owner's calendar date, `owner_zones`).
- `waiting`: the Cabinet's list, the user's waiting candidates newest first and the count of confirmed ones.
- `confirm` / `reject`: the owner's "True" / "Not true" on one of the owner's own candidates. A confirmation's edits (any of
  `text`, `happens_when`, `said_when`, `basis`) are checked before anything is written: a basis outside the three, a
  field that is not text, an empty text or one over CANDIDATE_MAX_WORDS answers INVALID with the reason. After a stored
  confirmation one `memory_inbox_final` task is enqueued FINAL_DELAY later, named by the `final_due_at` the confirmation
  set; a failed enqueue is logged and the answer stays DONE (the sweep's backstop finds the confirmed candidates).

Logs carry ids and counts, never a candidate's text (§12).
"""
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Sequence, Union

from ..domain.memory_inbox import (
    FINAL_DELAY,
    OWNER_FIELD_NAMES,
    AddReceipt,
    Answered,
    AnswerResult,
    Candidate,
    CandidateStatus,
    InboxView,
    SubmissionDraft,
    SubmittingClient,
    check_owner_fields,
    owner_day,
    parse_basis_strict,
)
from ..domain.memory_inbox_task_keys import FINAL_TASK, INBOX_DEADLINE_S, final_key, task_payload
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger

if TYPE_CHECKING:
    from .owner_timezone_resolver import OwnerTimezoneResolver


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryInboxService:
    def __init__(self, *, store: MemoryInboxRepository, task_queue: Optional[TaskQueue],
                 owner_zones: "OwnerTimezoneResolver", clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._task_queue = task_queue
        self._owner_zones = owner_zones
        self._clock = clock

    async def submit(self, *, user_id: str, account_id: str, client: SubmittingClient,
                     drafts: Sequence[SubmissionDraft]) -> AddReceipt:
        """Store `drafts` for the user at the clock's now, counted against the owner-local day."""
        now = self._clock()
        zone = (await self._owner_zones.resolve(account_id)).zone
        return await self._store.add_submissions(user_id, account_id, client, drafts, now, owner_day(now, zone))

    async def waiting(self, user_id: str) -> InboxView:
        candidates = await self._store.candidates(user_id, {CandidateStatus.CANDIDATE})
        confirmed = await self._store.count_candidates(user_id, CandidateStatus.CONFIRMED)
        return InboxView(candidates=tuple(candidates), confirmed=confirmed)

    async def confirm(self, user_id: str, candidate_id: str, edits: Mapping[str, Any]) -> Answered:
        parsed = _parse_edits(edits)
        if isinstance(parsed, Answered):
            logger.info(f"[MemoryInbox] confirm user={user_id[:8]} candidate={candidate_id} refused={parsed.reason}")
            return parsed
        # The owner's own candidates waiting for the owner: the one answered gives the triage fields an edit completes and
        # the account its final task runs for. Not among them: the store answers why (unknown, another user's, answered).
        candidate = await self._waiting_candidate(user_id, candidate_id)
        fields = replace(candidate.fields, **parsed) if candidate is not None and parsed else None
        if fields is not None:
            problem = check_owner_fields(fields)
            if problem is not None:
                logger.info(f"[MemoryInbox] confirm user={user_id[:8]} candidate={candidate_id} refused={problem}")
                return Answered(AnswerResult.INVALID, problem)
        now = self._clock()
        result = await self._store.confirm(candidate_id, user_id, fields, now)
        edited = candidate is not None and fields is not None and fields != candidate.fields
        logger.info(f"[MemoryInbox] confirm user={user_id[:8]} candidate={candidate_id} edited={int(edited)} "
                    f"result={result.value}")
        if result is AnswerResult.DONE:
            if candidate is None:
                logger.error(f"❌ [MemoryInbox] candidate {candidate_id} was confirmed but not read as waiting; "
                             f"no final task is enqueued (the sweep's backstop runs it)")
            else:
                await self._enqueue_final(user_id, candidate.account_id, now + FINAL_DELAY)
        return Answered(result)

    async def reject(self, user_id: str, candidate_id: str) -> Answered:
        result = await self._store.reject(candidate_id, user_id, self._clock())
        logger.info(f"[MemoryInbox] reject user={user_id[:8]} candidate={candidate_id} result={result.value}")
        return Answered(result)

    async def _waiting_candidate(self, user_id: str, candidate_id: str) -> Optional[Candidate]:
        waiting = await self._store.candidates(user_id, {CandidateStatus.CANDIDATE})
        return next((c for c in waiting if c.candidate_id == candidate_id), None)

    async def _enqueue_final(self, user_id: str, account_id: str, final_due_at: datetime) -> None:
        if self._task_queue is None:
            logger.warning(f"⚠️ [MemoryInbox] no task queue: the final pass of user {user_id[:8]} waits for the sweep")
            return
        try:
            await self._task_queue.enqueue_worker_task(
                FINAL_TASK, task_payload(user_id), delay_seconds=int(FINAL_DELAY.total_seconds()),
                deadline_seconds=INBOX_DEADLINE_S, dedup_key=final_key(user_id, final_due_at),
            )
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] final task of user {user_id[:8]} not enqueued: {type(exc).__name__}: {exc}; "
                         f"the sweep's backstop runs it")


def _parse_edits(edits: Mapping[str, Any]) -> Union[Dict[str, Any], Answered]:
    """The owner's edits as candidate field values (texts trimmed), or the INVALID answer naming the refused field. A
    missing or null field is not edited; any other key is ignored."""
    parsed: Dict[str, Any] = {}
    for name in OWNER_FIELD_NAMES:
        value = edits.get(name)
        if value is None:
            continue
        if name == "basis":
            basis = parse_basis_strict(value)
            if basis is None:
                return Answered(AnswerResult.INVALID, "basis")
            parsed[name] = basis
        elif not isinstance(value, str):
            return Answered(AnswerResult.INVALID, name)
        else:
            parsed[name] = value.strip()
    return parsed
