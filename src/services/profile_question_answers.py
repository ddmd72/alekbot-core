"""ProfileQuestionAnswers — the owner's answers to profile questions, recorded (fact profile RFC §4.7).

Written only from the owner's answer: a picked option (a digit or a button) is built into an answer by the domain
(`profile_option_answer`) and recorded in the account's answers store, where the first answer for a key wins; a key that
already has an answer is applied too (the owner's choice stands as first recorded). The effect on the profile is the
profile step's, by code, on its next run. A reply in words is read here in `apply_words`; the forms code reads are not
there yet, so every reply in words is not read and nothing is recorded."""
from datetime import datetime

from ..domain.fact_review_question import FactReviewQuestion
from ..domain.profile_apply import ProfileApply
from ..domain.profile_option_answer import profile_option_answer
from ..ports.profile_answer_repository import ProfileAnswerRepository
from ..utils.logger import logger


class ProfileQuestionAnswers:
    def __init__(self, answers: ProfileAnswerRepository) -> None:
        self._answers = answers

    async def apply_option(self, question: FactReviewQuestion, option_id: str, at: datetime) -> ProfileApply:
        """`APPLIED` when the option's answer was recorded or its key already had one; `NOT_READ` for an option that is none
        of the question's. Store errors raise."""
        answer = profile_option_answer(question, option_id, at)
        if answer is None:
            logger.info(f"[ProfileAnswers] question {question.question_id}: {option_id!r} is no option; nothing recorded")
            return ProfileApply.NOT_READ
        recorded = await self._answers.record(question.account_id, answer)
        logger.info(
            f"[ProfileAnswers] question {question.question_id}: {answer.resolution} "
            f"{'recorded' if recorded else 'already had an answer'}"
        )
        return ProfileApply.APPLIED

    async def apply_words(self, question: FactReviewQuestion, text: str, at: datetime) -> ProfileApply:
        """The owner's words read by code: not yet (the forms arrive with the questions being created), so `NOT_READ`."""
        return ProfileApply.NOT_READ
