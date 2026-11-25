from abc import ABC, abstractmethod
from datetime import datetime
from typing import AbstractSet, List, Optional, Sequence, Tuple

from ..domain.memory_inbox import (
    AddReceipt,
    AnswerResult,
    AppliedTriage,
    Candidate,
    CandidateFields,
    CandidateStatus,
    InboxState,
    SettledFinal,
    Submission,
    SubmissionDraft,
    SubmittingClient,
    TriageDecisions,
)


class MemoryInboxRepository(ABC):
    """The memory inbox (MCP_MEMORY_SUBMISSIONS_RFC §6): submissions and candidates in one store, and one state record per
    user.

    Every move of a record is a precondition write: it reads the record and writes only when the record is still in the
    status the move starts from, in the same atomic step, so two writers racing on one record (the sweep's expiry and
    the owner's "True", a redelivered triage) never overwrite each other. A record that closes gets its `purge_after`
    (`purge_after_of`) in that step; an open record never carries one. Backend errors propagate.
    """

    @abstractmethod
    async def add_submissions(self, user_id: str, account_id: str, client: SubmittingClient,
                              drafts: Sequence[SubmissionDraft], now: datetime, day: str) -> AddReceipt:
        """Store the drafts as `received` submissions at `now`, in order, as far as the user's count for the owner-local
        `day` allows (RECEIVED_PER_DAY); the rest are not stored and counted as `over_day`. The count lives on the state
        record, updated in the same atomic step; a new `day` starts it again at zero. No drafts: nothing is written."""

    @abstractmethod
    async def received(self, user_id: str, limit: int) -> List[Submission]:
        """The user's `received` submissions, oldest first by `received_at`, at most `limit`."""

    @abstractmethod
    async def candidates(self, user_id: str, statuses: AbstractSet[CandidateStatus],
                         limit: Optional[int] = None) -> List[Candidate]:
        """The user's candidates in any of `statuses`, newest first by `created_at`, at most `limit` (None: all)."""

    @abstractmethod
    async def count_candidates(self, user_id: str, status: CandidateStatus) -> int:
        """How many of the user's candidates are in `status`."""

    @abstractmethod
    async def apply_triage(self, user_id: str, account_id: str, sent_ids: Sequence[str],
                           decisions: TriageDecisions, language: str, now: datetime) -> AppliedTriage:
        """Write one triage pass's decisions over the submissions it was sent, in one atomic step.

        Only a sent id that is still a `received` submission of `user_id` is touched; any other id (unknown, another
        user's, already settled) is left as it is. Each candidate is created `candidate` at `now` with `answer_by` =
        `now` + ANSWER_WINDOW, `language`, and the copies of its sources; its sources are its cited ids that are touched,
        and a candidate with none is not created. A cited submission becomes `triaged`; a discarded one (not cited:
        citing wins) becomes `discarded` with its reason; both get `triaged_at` = `now` and their `purge_after`. Every
        other touched submission gets one more `triage_offers` and, at TRIAGE_MAX_OFFERS, is `discarded` `undecided`.
        """

    @abstractmethod
    async def confirm(self, candidate_id: str, user_id: str, fields: Optional[CandidateFields],
                      now: datetime) -> AnswerResult:
        """The owner's "True": `candidate` -> `confirmed` at `now`. `fields` (None: unchanged) become the candidate's;
        triage's four stay in `proposed`; `edited` says whether they differ. In the same atomic step the user's state
        gets `final_due_at` = `now` + FINAL_DELAY and `final_attempts` = 0 (a confirmation restarts the burst's
        run retries; each candidate's own FINAL_MAX_ATTEMPTS still bounds the work). NOT_FOUND (no such candidate), OTHER_USER, NOT_WAITING (not in
        `candidate`) write nothing."""

    @abstractmethod
    async def reject(self, candidate_id: str, user_id: str, now: datetime) -> AnswerResult:
        """The owner's "Not true": `candidate` -> `rejected` at `now`, with its `purge_after`. Refusals as `confirm`."""

    @abstractmethod
    async def settle_final(self, candidate_ids: Sequence[str], ended: bool, now: datetime) -> SettledFinal:
        """After a final pass over `candidate_ids`. Only a candidate still `confirmed` is touched. `ended` (the pass gave
        its final report): each becomes `consolidated` at `now`, outcome `done`. Not ended: each gets one more
        `final_attempts` and, at FINAL_MAX_ATTEMPTS, becomes `consolidated` at `now`, outcome `gave_up`. Both get their
        `purge_after` when they become `consolidated`."""

    @abstractmethod
    async def expire_due(self, now: datetime) -> int:
        """Every `candidate` whose `answer_by` <= `now`, of any user, becomes `expired` with its `purge_after`, each by
        its own precondition write; how many did."""

    @abstractmethod
    async def users_with_received(self) -> List[Tuple[str, str]]:
        """Every (user_id, account_id) holding a `received` submission, each once."""

    @abstractmethod
    async def users_final_overdue(self, before: datetime) -> List[str]:
        """Every user whose state's `final_due_at` is before `before`."""

    @abstractmethod
    async def state(self, user_id: str) -> InboxState:
        """The user's state; an empty one when none is stored."""

    @abstractmethod
    async def record_triage(self, user_id: str, ok: bool, now: datetime) -> None:
        """A triage run ended at `now`: `last_triage_at` when `ok`, else `last_triage_failed_at`."""

    @abstractmethod
    async def clear_final_due(self, user_id: str, expected: datetime, now: datetime) -> bool:
        """A final run that read `final_due_at` == `expected` ended at `now`: sets `last_final_at` = `now`, and only when
        `final_due_at` is still `expected` clears it and `final_attempts` (True). A later confirmation moved it: it stays,
        with its own pending task (False)."""

    @abstractmethod
    async def bump_final_retry(self, user_id: str) -> int:
        """One more failed final run of the current burst: the state's `final_attempts` after the increment."""
