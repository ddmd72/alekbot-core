"""ProfileQuestionAnswers — the owner's answers to profile questions, recorded (fact profile RFC §4.7).

Written only from the owner's answer: a picked option (a digit or a button) is built into an answer by the domain
(`profile_option_answer`) and recorded in the account's answers store, where the first answer for a key wins; a key that
already has an answer is applied too (the owner's choice stands as first recorded). The effect on the profile is the
profile step's, by code, on its next run. A reply in words is read here in `apply_words` by the forms of
`profile_reply_words` (a place, a person from the confirmed list, a date); a reply that is none of them is not read and
nothing is recorded. The atlas and the confirmed list are read only for a place or a person reply, so a service wired
without them reads dates only."""
from datetime import datetime
from typing import Awaitable, Callable, Optional, Tuple

from ..domain.fact_review_question import FactReviewQuestion
from ..domain.place_names import PlaceNames
from ..domain.profile_answers import OwnerAnswer, PersonAnswer, PlaceAnswer
from ..domain.profile_apply import ProfileApply
from ..domain.profile_option_answer import profile_option_answer
from ..domain.profile_reply_words import read_date_reply, read_person_reply, read_place_reply
from ..ports.confirmed_people_repository import ConfirmedPeopleRepository
from ..ports.profile_answer_repository import ProfileAnswerRepository
from ..utils.logger import logger

# The place names and their digest, loaded once by the composition root (the profile step shares the same loader).
Atlas = Callable[[], Awaitable[Tuple[PlaceNames, str]]]


class ProfileQuestionAnswers:
    def __init__(
        self,
        answers: ProfileAnswerRepository,
        atlas: Optional[Atlas] = None,
        confirmed_people: Optional[ConfirmedPeopleRepository] = None,
    ) -> None:
        self._answers = answers
        self._atlas = atlas
        self._confirmed_people = confirmed_people

    async def apply_option(self, question: FactReviewQuestion, option_id: str, at: datetime) -> ProfileApply:
        """`APPLIED` when the option's answer was recorded or its key already had one; `NOT_READ` for an option that is none
        of the question's. Store errors raise."""
        answer = profile_option_answer(question, option_id, at)
        if answer is None:
            logger.info(f"[ProfileAnswers] question {question.question_id}: {option_id!r} is no option; nothing recorded")
            return ProfileApply.NOT_READ
        return await self._record(question, answer)

    async def _record(self, question: FactReviewQuestion, answer: OwnerAnswer) -> ProfileApply:
        recorded = await self._answers.record(question.account_id, answer)
        logger.info(
            f"[ProfileAnswers] question {question.question_id}: {answer.resolution} "
            f"{'recorded' if recorded else 'already had an answer'}"
        )
        return ProfileApply.APPLIED

    async def apply_words(self, question: FactReviewQuestion, text: str, at: datetime) -> ProfileApply:
        """The owner's words read by code (`profile_reply_words`): `APPLIED` when they are a form of the question's kind
        and the answer was recorded or its key already had one; `NOT_READ` otherwise, nothing recorded. Store errors
        raise."""
        answer = await self._read_words(question, text, at)
        if answer is None:
            logger.info(f"[ProfileAnswers] question {question.question_id}: the words are not a form read; nothing recorded")
            return ProfileApply.NOT_READ
        return await self._record(question, answer)

    async def _read_words(self, question: FactReviewQuestion, text: str, at: datetime) -> Optional[OwnerAnswer]:
        point = question.open_point
        if point is None:
            return None
        qid = question.question_id
        if question.question == "date":
            return read_date_reply(text, point, qid, at)
        if question.question == "place":
            if self._atlas is None:
                return None
            names, _ = await self._atlas()
            place = read_place_reply(text, point, names)
            if place is None:
                return None
            if place.resolution == "atlas":
                return PlaceAnswer(key=point.key, city=point.city, country=point.country, resolution="atlas",
                                   geonameid=place.geonameid, question_id=qid, answered_at=at)
            return PlaceAnswer(key=point.key, city=place.city, country=place.country, resolution="local",
                               question_id=qid, answered_at=at)
        if question.question == "person":
            if self._confirmed_people is None:
                return None
            person = read_person_reply(text, point, await self._confirmed_people.get(question.account_id))
            if person is None:
                return None
            return PersonAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention, resolution="entry",
                                name=person.name, question_id=qid, answered_at=at)
        return None
