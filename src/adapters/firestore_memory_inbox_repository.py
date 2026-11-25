"""
FirestoreMemoryInboxRepository — the memory inbox on Firestore (MCP_MEMORY_SUBMISSIONS_RFC §6).

`{prefix}memory_inbox/{auto id}`: a `Submission` or a `Candidate` as its own `to_record()` (the kinds share the one
`status` field, whose values never overlap). `purge_after` exists only on a closed record (the collection's TTL policy);
no field is named `expires_at`. `{prefix}memory_inbox_state/{user_id}`: an `InboxState` as its `to_record()`, written
field by field (`set(..., merge=True)`), so each writer touches only its own fields.

Every move reads the record in a transaction and writes it only when it is still in the status the move starts from:
an aborted commit re-runs the body on what the retry reads (the client's retries), so of two writers racing on one
record the second sees the first's result. A record whose document no longer reads as its kind is skipped (logged) by
a list and left alone by a move. Logs carry ids and counts, never a record's text.

Queries: the triage batch `(user_id, status, received_at ASC)`, the lists `(user_id, status in, created_at DESC)` and the
sweep's expiry `(status, answer_by)` use the composites of `config/firestore.indexes.json`; the received users (one
equality and a projection), the counts and the overdue state (one range) use single-field indexes.
"""
from datetime import datetime
from typing import AbstractSet, Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.memory_inbox import (
    ANSWER_WINDOW,
    FINAL_DELAY,
    FINAL_MAX_ATTEMPTS,
    RECEIVED_PER_DAY,
    TRIAGE_MAX_OFFERS,
    AddReceipt,
    AnswerResult,
    AppliedTriage,
    Candidate,
    CandidateFields,
    CandidateStatus,
    DiscardReason,
    FinalOutcomeKind,
    InboxState,
    SettledFinal,
    SourceCopy,
    Submission,
    SubmissionDraft,
    SubmissionStatus,
    SubmittingClient,
    TriageDecisions,
    purge_after_of,
)
from ..ports.memory_inbox_repository import MemoryInboxRepository
from ..utils.logger import logger

T = TypeVar("T")


class FirestoreMemoryInboxRepository(MemoryInboxRepository):
    def __init__(self, db_client: Any, env_config: EnvironmentConfig) -> None:
        self._db = db_client
        self._inbox = db_client.collection(env_config.memory_inbox_collection)
        self._states = db_client.collection(env_config.memory_inbox_state_collection)

    # ------------------------------------------------------------------------------------------------ submissions

    async def add_submissions(self, user_id: str, account_id: str, client: SubmittingClient,
                              drafts: Sequence[SubmissionDraft], now: datetime, day: str) -> AddReceipt:
        if not user_id or not account_id:
            raise ValueError("user_id and account_id are required to store submissions")
        if not drafts:
            return AddReceipt(stored=0, over_day=0)
        state_ref = self._states.document(user_id)

        async def body(transaction: Any) -> AddReceipt:
            snap = await state_ref.get(transaction=transaction)
            state = self._the_state(user_id, snap)
            count = state.received_day[1] if state.received_day is not None and state.received_day[0] == day else 0
            kept = list(drafts)[:max(RECEIVED_PER_DAY - count, 0)]
            for draft in kept:
                submission = Submission(
                    submission_id="", user_id=user_id, account_id=account_id, status=SubmissionStatus.RECEIVED,
                    received_at=now, client_id=client.client_id, client_name=client.client_name,
                    information=draft.information, basis=draft.basis, said_when=draft.said_when,
                    happens_when=draft.happens_when, cut=draft.cut,
                )
                transaction.create(self._inbox.document(), submission.to_record())
            if kept:
                transaction.set(state_ref, {"user_id": user_id, "received_day": {"day": day, "count": count + len(kept)}},
                                merge=True)
            return AddReceipt(stored=len(kept), over_day=len(drafts) - len(kept))

        return await self._in_transaction(body, f"submissions of user {user_id[:8]}")

    async def received(self, user_id: str, limit: int) -> List[Submission]:
        query = (self._inbox.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("status", "==", SubmissionStatus.RECEIVED.value))
                 .order_by("received_at").limit(limit))
        return [s for s in (self._submission(snap) for snap in await query.get()) if s is not None]

    # ------------------------------------------------------------------------------------------------ candidates

    async def candidates(self, user_id: str, statuses: AbstractSet[CandidateStatus],
                         limit: Optional[int] = None) -> List[Candidate]:
        if not statuses:
            return []
        query = (self._inbox.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("status", "in", sorted(s.value for s in statuses)))
                 .order_by("created_at", direction=firestore.Query.DESCENDING))
        if limit is not None:
            query = query.limit(limit)
        return [c for c in (self._candidate(snap) for snap in await query.get()) if c is not None]

    async def count_candidates(self, user_id: str, status: CandidateStatus) -> int:
        query = (self._inbox.where(filter=FieldFilter("user_id", "==", user_id))
                 .where(filter=FieldFilter("status", "==", status.value)))
        result = await query.count(alias="n").get()
        return int(result[0][0].value)

    # ------------------------------------------------------------------------------------------------ triage

    async def apply_triage(self, user_id: str, account_id: str, sent_ids: Sequence[str],
                           decisions: TriageDecisions, language: str, now: datetime) -> AppliedTriage:
        sent = list(dict.fromkeys(i for i in sent_ids if i))
        if not sent:
            return AppliedTriage(candidates=0, triaged=0, discarded=0, offered=0, undecided=0)

        async def body(transaction: Any) -> AppliedTriage:
            waiting: Dict[str, Submission] = {}
            async for snap in self._db.get_all([self._inbox.document(i) for i in sent], transaction=transaction):
                submission = self._submission(snap, quiet=True) if snap.exists else None
                if (submission is not None and submission.user_id == user_id
                        and submission.status is SubmissionStatus.RECEIVED):
                    waiting[snap.id] = submission
            cited: Set[str] = set()
            made = 0
            for draft in decisions.candidates:
                ids = [i for i in dict.fromkeys(draft.source_ids) if i in waiting]
                if not ids:
                    continue
                candidate = Candidate(
                    candidate_id="", user_id=user_id, account_id=account_id, status=CandidateStatus.CANDIDATE,
                    created_at=now, answer_by=now + ANSWER_WINDOW, language=language, source_ids=tuple(ids),
                    sources=tuple(_copy(waiting[i]) for i in ids), text=draft.fields.text,
                    happens_when=draft.fields.happens_when, said_when=draft.fields.said_when, basis=draft.fields.basis,
                    draft=draft.draft, relation=draft.relation,
                )
                transaction.create(self._inbox.document(), candidate.to_record())
                cited.update(ids)
                made += 1
            triaged = discarded = offered = undecided = 0
            for submission_id, submission in waiting.items():
                ref = self._inbox.document(submission_id)
                reason = decisions.discards.get(submission_id)
                if submission_id in cited:
                    transaction.update(ref, _settled(SubmissionStatus.TRIAGED, None, now))
                    triaged += 1
                elif reason is not None:
                    transaction.update(ref, _settled(SubmissionStatus.DISCARDED, reason, now))
                    discarded += 1
                elif submission.triage_offers + 1 >= TRIAGE_MAX_OFFERS:
                    transaction.update(ref, {**_settled(SubmissionStatus.DISCARDED, DiscardReason.UNDECIDED, now),
                                             "triage_offers": submission.triage_offers + 1})
                    undecided += 1
                else:
                    transaction.update(ref, {"triage_offers": submission.triage_offers + 1})
                    offered += 1
            return AppliedTriage(candidates=made, triaged=triaged, discarded=discarded, offered=offered,
                                 undecided=undecided)

        applied = await self._in_transaction(body, f"triage of user {user_id[:8]}")
        logger.info(f"[MemoryInboxStore] triage user={user_id[:8]} sent={len(sent)} candidates={applied.candidates} "
                    f"triaged={applied.triaged} discarded={applied.discarded} offered={applied.offered} "
                    f"undecided={applied.undecided}")
        return applied

    # ------------------------------------------------------------------------------------------------ the owner's answers

    async def confirm(self, candidate_id: str, user_id: str, fields: Optional[CandidateFields],
                      now: datetime) -> AnswerResult:
        state_ref = self._states.document(user_id)

        def move(candidate: Candidate) -> Dict[str, Any]:
            chosen = candidate.fields if fields is None else fields
            return {"status": CandidateStatus.CONFIRMED.value, "confirmed_at": now, **chosen.to_record(),
                    "proposed": candidate.fields.to_record(), "edited": chosen != candidate.fields}

        def state_write(transaction: Any) -> None:
            transaction.set(state_ref, {"user_id": user_id, "final_due_at": now + FINAL_DELAY, "final_attempts": 0}, merge=True)

        return await self._answer(candidate_id, user_id, move, state_write)

    async def reject(self, candidate_id: str, user_id: str, now: datetime) -> AnswerResult:
        def move(candidate: Candidate) -> Dict[str, Any]:
            return {"status": CandidateStatus.REJECTED.value, "rejected_at": now,
                    "purge_after": purge_after_of(CandidateStatus.REJECTED, now)}

        return await self._answer(candidate_id, user_id, move, None)

    # ------------------------------------------------------------------------------------------------ final pass

    async def settle_final(self, candidate_ids: Sequence[str], ended: bool, now: datetime) -> SettledFinal:
        wanted = list(dict.fromkeys(i for i in candidate_ids if i))
        if not wanted:
            return SettledFinal(consolidated=0, retried=0, gave_up=())

        async def body(transaction: Any) -> SettledFinal:
            confirmed: Dict[str, Candidate] = {}
            async for snap in self._db.get_all([self._inbox.document(i) for i in wanted], transaction=transaction):
                candidate = self._candidate(snap, quiet=True) if snap.exists else None
                if candidate is not None and candidate.status is CandidateStatus.CONFIRMED:
                    confirmed[snap.id] = candidate
            done = retried = 0
            gave_up: List[str] = []
            for candidate_id in wanted:
                candidate = confirmed.get(candidate_id)
                if candidate is None:
                    continue
                ref = self._inbox.document(candidate_id)
                if ended:
                    transaction.update(ref, _consolidated(FinalOutcomeKind.DONE, now))
                    done += 1
                    continue
                attempts = candidate.final_attempts + 1
                if attempts >= FINAL_MAX_ATTEMPTS:
                    transaction.update(ref, {**_consolidated(FinalOutcomeKind.GAVE_UP, now), "final_attempts": attempts})
                    gave_up.append(candidate_id)
                else:
                    transaction.update(ref, {"final_attempts": attempts})
                    retried += 1
            return SettledFinal(consolidated=done, retried=retried, gave_up=tuple(gave_up))

        return await self._in_transaction(body, f"final settlement of {len(wanted)} candidates")

    # ------------------------------------------------------------------------------------------------ sweep

    async def expire_due(self, now: datetime) -> int:
        query = (self._inbox.where(filter=FieldFilter("status", "==", CandidateStatus.CANDIDATE.value))
                 .where(filter=FieldFilter("answer_by", "<=", now)))
        expired = 0
        for snap in await query.get():
            if await self._expire(snap.id, now):
                expired += 1
        return expired

    async def users_with_received(self) -> List[Tuple[str, str]]:
        query = (self._inbox.where(filter=FieldFilter("status", "==", SubmissionStatus.RECEIVED.value))
                 .select(["user_id", "account_id"]))
        found: Dict[Tuple[str, str], None] = {}
        for snap in await query.get():
            data = snap.to_dict() or {}
            user_id, account_id = data.get("user_id"), data.get("account_id")
            if isinstance(user_id, str) and user_id and isinstance(account_id, str) and account_id:
                found[(user_id, account_id)] = None
        return list(found)

    async def users_final_overdue(self, before: datetime) -> List[str]:
        query = self._states.where(filter=FieldFilter("final_due_at", "<", before))
        return [snap.id for snap in await query.get()]

    # ------------------------------------------------------------------------------------------------ state

    async def state(self, user_id: str) -> InboxState:
        return self._the_state(user_id, await self._states.document(user_id).get())

    async def record_triage(self, user_id: str, ok: bool, now: datetime) -> None:
        field = "last_triage_at" if ok else "last_triage_failed_at"
        await self._states.document(user_id).set({"user_id": user_id, field: now}, merge=True)

    async def clear_final_due(self, user_id: str, expected: datetime, now: datetime) -> bool:
        state_ref = self._states.document(user_id)

        async def body(transaction: Any) -> bool:
            state = self._the_state(user_id, await state_ref.get(transaction=transaction))
            still = state.final_due_at is not None and state.final_due_at == expected
            write: Dict[str, Any] = {"user_id": user_id, "last_final_at": now}
            if still:
                write.update({"final_due_at": None, "final_attempts": 0})
            transaction.set(state_ref, write, merge=True)
            return still

        return await self._in_transaction(body, f"final due of user {user_id[:8]}")

    async def bump_final_retry(self, user_id: str) -> int:
        state_ref = self._states.document(user_id)

        async def body(transaction: Any) -> int:
            state = self._the_state(user_id, await state_ref.get(transaction=transaction))
            attempts = state.final_attempts + 1
            transaction.set(state_ref, {"user_id": user_id, "final_attempts": attempts}, merge=True)
            return attempts

        return await self._in_transaction(body, f"final retry of user {user_id[:8]}")

    # ------------------------------------------------------------------------------------------------ plumbing

    async def _answer(self, candidate_id: str, user_id: str, move: Callable[[Candidate], Dict[str, Any]],
                      also: Optional[Callable[[Any], None]]) -> AnswerResult:
        """Read the candidate, refuse (NOT_FOUND, OTHER_USER, NOT_WAITING) or write `move(candidate)` and `also`'s
        writes, in one transaction."""
        if not candidate_id or not user_id:
            return AnswerResult.NOT_FOUND
        ref = self._inbox.document(candidate_id)

        async def body(transaction: Any) -> AnswerResult:
            snap = await ref.get(transaction=transaction)
            candidate = self._candidate(snap, quiet=True) if snap.exists else None
            if candidate is None:
                return AnswerResult.NOT_FOUND
            if candidate.user_id != user_id:
                return AnswerResult.OTHER_USER
            if candidate.status is not CandidateStatus.CANDIDATE:
                return AnswerResult.NOT_WAITING
            transaction.update(ref, move(candidate))
            if also is not None:
                also(transaction)
            return AnswerResult.DONE

        return await self._in_transaction(body, f"candidate {candidate_id[:12]}")

    async def _expire(self, candidate_id: str, now: datetime) -> bool:
        """One candidate `candidate` -> `expired`, only while it is still waiting and due."""
        ref = self._inbox.document(candidate_id)

        async def body(transaction: Any) -> bool:
            snap = await ref.get(transaction=transaction)
            candidate = self._candidate(snap, quiet=True) if snap.exists else None
            if candidate is None or candidate.status is not CandidateStatus.CANDIDATE or candidate.answer_by > now:
                return False
            transaction.update(ref, {"status": CandidateStatus.EXPIRED.value,
                                     "purge_after": purge_after_of(CandidateStatus.EXPIRED, now)})
            return True

        return await self._in_transaction(body, f"expiry of candidate {candidate_id[:12]}")

    def _submission(self, snap: Any, quiet: bool = False) -> Optional[Submission]:
        try:
            return Submission.from_record(snap.id, snap.to_dict() or {})
        except ValueError as exc:
            if not quiet:
                logger.error(f"❌ [MemoryInboxStore] submission {snap.id[:12]} does not read and is skipped: {exc}")
            return None

    def _candidate(self, snap: Any, quiet: bool = False) -> Optional[Candidate]:
        try:
            return Candidate.from_record(snap.id, snap.to_dict() or {})
        except ValueError as exc:
            if not quiet:
                logger.error(f"❌ [MemoryInboxStore] candidate {snap.id[:12]} does not read and is skipped: {exc}")
            return None

    @staticmethod
    def _the_state(user_id: str, snap: Any) -> InboxState:
        """The user's state (empty when none is stored); an unreadable one is logged and raised, never overwritten."""
        if not snap.exists:
            return InboxState(user_id=user_id)
        try:
            return InboxState.from_record(user_id, snap.to_dict() or {})
        except ValueError:
            logger.error(f"❌ [MemoryInboxStore] inbox state {user_id[:8]} does not read; nothing is applied")
            raise

    async def _in_transaction(self, body: Callable[[Any], Awaitable[T]], what: str) -> T:
        """Run `body` in a fresh transaction; an aborted commit re-runs it (the client's retries), so every decision is
        taken again on what the retry reads. An error after the last attempt is logged and raised."""
        @firestore.async_transactional
        async def run(transaction: Any) -> T:
            return await body(transaction)

        try:
            return await run(self._db.transaction())
        except Exception as exc:
            logger.error(f"❌ [MemoryInboxStore] transaction on {what} failed: {type(exc).__name__}: {exc}")
            raise


def _copy(submission: Submission) -> SourceCopy:
    return SourceCopy(submission_id=submission.submission_id, received_at=submission.received_at,
                      client_name=submission.client_name, information=submission.information, basis=submission.basis,
                      said_when=submission.said_when, happens_when=submission.happens_when)


def _settled(status: SubmissionStatus, reason: Optional[DiscardReason], now: datetime) -> Dict[str, Any]:
    return {"status": status.value, "triaged_at": now, "discard_reason": None if reason is None else reason.value,
            "purge_after": purge_after_of(status, now)}


def _consolidated(outcome: FinalOutcomeKind, now: datetime) -> Dict[str, Any]:
    return {"status": CandidateStatus.CONSOLIDATED.value, "consolidated_at": now, "outcome": outcome.value,
            "purge_after": purge_after_of(CandidateStatus.CONSOLIDATED, now)}

