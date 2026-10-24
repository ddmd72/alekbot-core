"""
FactReviewStore — the memory review's questions and its per-account state (Facts S4 plan §2.2, §3.7, §3.13).

The store decides no transition. Every status change is a method of `FactReviewQuestion`; a write reads the record in a
transaction, calls that method and writes the result. A move the method refuses (`QuestionTransitionError`) is the
write's `False`, never an exception to the caller. `False` also means the question does not exist. A stored document
the model no longer validates is skipped (logged as ERROR) by the list reads; for the one question or state a write is
about, it is logged as ERROR and raised, because answering "refused" would hide it. Store errors raise.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional, Sequence

from ..domain.fact_review_answer_kind import AnswerKind
from ..domain.fact_review_question import AnswerSource, ClosedBy, FactReviewQuestion
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
    async def by_message(self, user_id: str, platform: str, channel_id: str, message_id: str) -> Optional[FactReviewQuestion]:
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
