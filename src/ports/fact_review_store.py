"""
FactReviewStore — the memory review's questions and its per-account state (Facts S4 plan §2.2, §3.7, §3.13).

The queue and the current question: `claim_next` is the one way a question goes out, deciding with the domain's
`current_question`, `may_send`, `next_in_queue` and `sent_today` inside one transaction over the state document and the
account's open questions, so two concurrent claims give one question. The writes that end an attempt
(`requeue_front`, `mark_unanswered`, `close_and_release`) clear the state's `current_question_id` in the same
transaction, and only when it names that question.

The store decides no transition. Every status change is a method of `FactReviewQuestion`; a write reads the record in a
transaction, calls that method and writes the result. A move the method refuses (`QuestionTransitionError`) is the
write's `False`, never an exception to the caller. `False` also means the question does not exist. A stored document
the model no longer validates is skipped (logged as ERROR) by the list reads; for the one question or state a write is
about, it is logged as ERROR and raised, because answering "refused" would hide it. Store errors raise.
"""
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import List, Optional, Sequence

from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_question import AnswerSource, ClosedBy, FactReviewQuestion
from ..domain.fact_review_state import FactReviewState
from ..domain.messaging import MessageRef


class FactReviewStore(ABC):
    # ------------------------------------------------------------------------------------------------ reads

    @abstractmethod
    async def get_questions(self, ids: Sequence[str]) -> List[FactReviewQuestion]:
        """The questions with these ids, read in one `get_all`, in the order of `ids` (a repeated id once). A missing id
        is left out. No transaction."""

    @abstractmethod
    async def questions_for_account(self, account_id: str) -> List[FactReviewQuestion]:
        """The account's open questions: `account_id ==` and `status in [queued, pending, asked, unanswered]`, served by
        single-field indexes. Unordered: queue order is `queue_order`'s. No transaction."""

    @abstractmethod
    async def all_open(self) -> List[FactReviewQuestion]:
        """Every open question of every account: `status in [queued, pending, asked, unanswered]`. Unordered. No
        transaction."""

    @abstractmethod
    async def by_message(
        self, user_id: str, platform: str, channel_id: str, message_id: str,
    ) -> Optional[FactReviewQuestion]:
        """The question posted at that message, in any status: `user_id`, `message_platform`, `message_channel` and
        `message_id` all equal. The same message id in another channel, platform or user's question is not it (None).
        No transaction."""

    # ------------------------------------------------------------------------------------------------ question writes

    @abstractmethod
    async def set_message_ref(self, question_id: str, ref: MessageRef) -> bool:
        """`posted(ref)`, in a transaction over the question. False: no such question, or not a pending, appended,
        not yet posted one."""

    @abstractmethod
    async def record_reply(self, question_id: str, text: str, at: datetime) -> bool:
        """`with_reply(text, at)`, in a transaction over the question. True only when the question changed. False: no
        such question; not an asked or unanswered one, or its reply was already shown to the review; no words; or
        nothing more fits under `MAX_REPLY_CHARS` (`with_reply` returns the question unchanged)."""

    @abstractmethod
    async def mark_offered(self, question_id: str, at: datetime) -> bool:
        """`offered(at)`, in a transaction over the question. False: no such question, or it has no reply the review
        has not been shown."""

    @abstractmethod
    async def close(
        self,
        question_id: str,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        at: datetime,
    ) -> bool:
        """`closed(closed_by, at, answer, source)`, in a transaction over the question only: the state's pointer is not
        touched (`close_and_release` does that). `purge_after` comes with the close. False: no such question, it is
        closed already, or the answer and source do not fit the closer."""

    # ------------------------------------------------------------------------------------ queue and current question

    @abstractmethod
    async def create_queued(self, question: FactReviewQuestion) -> bool:
        """Create the question's document (a `create`, no transaction). False when a document with its id exists; the
        stored one is left as it is. Raises ValueError for a question that is not `queued`."""

    @abstractmethod
    async def get_state(self, account_id: str) -> Optional[FactReviewState]:
        """The account's review state, or None when it has none. No transaction."""

    @abstractmethod
    async def claim_next(self, account_id: str, today: date, session_id: str) -> Optional[FactReviewQuestion]:
        """Claim the next question to send, in one transaction over the state document and the account's open
        questions: `current_question`, then `may_send(state, today, current)`, then `next_in_queue`; when allowed, the
        question is `claimed(session_id)`, the state's `current_question_id` names it and `sent` becomes
        `sent_today(state, today) + 1` with `sent_on = today` (a new day starts at 1). An account without a state
        document starts from an empty state. Returns the claimed question; None when one is out, today's sends are used
        up, a delivery failed today or nothing is queued. A commit that loses to a concurrent writer re-runs and decides
        again on what it then reads."""

    @abstractmethod
    async def mark_appended(self, question_id: str, at: datetime) -> bool:
        """`appended(at)`, in a transaction over the question. False: no such question, or not a pending one whose frame
        is still to be appended."""

    @abstractmethod
    async def mark_asked(self, question_id: str, at: datetime) -> bool:
        """`asked(at)`, in a transaction over the question. False: no such question, or not pending and posted (one the
        owner closed meanwhile stays closed)."""

    @abstractmethod
    async def complete_delivered(self, question_id: str) -> bool:
        """`completed()`, in a transaction over the question: a pending question with a message ref becomes asked at
        its `appended_at`. False: no such question, or not pending and posted."""

    @abstractmethod
    async def requeue_front(self, question_id: str, failed_today: bool) -> bool:
        """`requeued()` (its `queue_key` kept, the attempt forgotten), in one transaction over the question and its
        account's state: the state's `current_question_id` is cleared when it names this question, and with
        `failed_today` `delivery_failed_on` becomes the state's `sent_on` (the owner-local day of the claim). False: no
        such question, or not a pending one that was never posted; the state is then untouched."""

    @abstractmethod
    async def mark_unanswered(self, question_id: str) -> bool:
        """`unanswered()`, in one transaction over the question and its account's state, clearing `current_question_id`
        when it names this question. False: no such question, or not an asked one; the state is then untouched."""

    @abstractmethod
    async def close_and_release(
        self,
        question_id: str,
        closed_by: ClosedBy,
        answer: Optional[AnswerKind],
        source: Optional[AnswerSource],
        at: datetime,
    ) -> bool:
        """`closed(closed_by, at, answer, source)`, in one transaction over the question and its account's state,
        clearing `current_question_id` only when it names this question. False as for `close`; the state is then
        untouched."""
