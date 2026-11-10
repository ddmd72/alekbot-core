"""The answer a picked option of a profile question gives (fact profile RFC §4.7): pure, so the digit path and the button
path read one rule. `pick1`…`pick3` pick the candidate the question carries (an atlas row by geonameid, a list entry by
name); `keep` keeps a place as written (`local`); `not_listed` says the person is on no list entry; `dont_know` records
`unknown`, so the point is never asked again. Any other id, or an option the kind does not offer, gives None."""
from datetime import datetime
from typing import Optional

from .fact_review_question import FactReviewQuestion
from .profile_answers import DateAnswer, OwnerAnswer, PersonAnswer, PlaceAnswer
from .profile_question_message import DONT_KNOW_OPTION, KEEP_OPTION, NOT_LISTED_OPTION


def profile_option_answer(question: FactReviewQuestion, option_id: str, at: datetime) -> Optional[OwnerAnswer]:
    """The owner's answer for `option_id` on `question`, or None when the option is not one the question offers. The key,
    the words and the candidates come from the question's open point; `question_id` and `answered_at` are set."""
    point = question.open_point
    if point is None:
        return None
    pick = next((option for option in point.options if option.id == option_id), None)
    qid = question.question_id
    if question.question == "place":
        if pick is not None:
            return PlaceAnswer(key=point.key, city=point.city, country=point.country, resolution="atlas",
                               geonameid=pick.value, question_id=qid, answered_at=at)
        if option_id == KEEP_OPTION:
            return PlaceAnswer(key=point.key, city=point.city, country=point.country, resolution="local",
                               question_id=qid, answered_at=at)
        if option_id == DONT_KNOW_OPTION:
            return PlaceAnswer(key=point.key, city=point.city, country=point.country, resolution="unknown",
                               question_id=qid, answered_at=at)
    elif question.question == "person":
        if pick is not None:
            return PersonAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention, resolution="entry",
                                name=pick.value, question_id=qid, answered_at=at)
        if option_id == NOT_LISTED_OPTION:
            return PersonAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention,
                                resolution="not_listed", question_id=qid, answered_at=at)
        if option_id == DONT_KNOW_OPTION:
            return PersonAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention,
                                resolution="unknown", question_id=qid, answered_at=at)
    elif question.question == "date" and option_id == DONT_KNOW_OPTION:
        return DateAnswer(key=point.key, lineage_id=point.lineage_id, mention=point.mention, resolution="unknown",
                          question_id=qid, answered_at=at)
    return None
