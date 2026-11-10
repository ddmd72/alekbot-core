"""Which profile questions the daily review creates and closes (fact profile RFC §4.7): pure.

`plan_profile_questions(points, open_questions, cap)` compares the open points of the account's profiles (most facts
first) with the profile questions already open: a question whose key is no point any more (answered, placed by an atlas
update, its facts no longer current) is to close `resolved_elsewhere`; a point with no open question is a candidate, in
the order of the points. At most `cap` profile questions are open: `slots` is how many more may be created. The
candidates are all of them and not only `slots`, because a point whose question was closed before still has its derived
id in the store and is refused on creation: the caller creates candidates until the slots are used.

`profile_question(point, account_id, user_id, at)` is the queued record of one point: the id derived from the account and
the key, the newest fact quoted, the kind of the point's field, no texts, fallback `none`, origin `profile`, the line in the
queue from `at`."""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Final, List, Mapping, Sequence, Tuple

from .fact_review_question import FactReviewQuestion, QuestionKind
from .fact_review_rules import PROFILE_OPEN_CAP, profile_question_id
from .profile_open_points import OpenPoint

_KIND_OF_FIELD: Final[Mapping[str, QuestionKind]] = {"places": "place", "people": "person", "time_points": "date"}


@dataclass(frozen=True)
class ProfileQuestionPlan:
    """`to_close`: open profile questions whose point is gone; `to_queue`: the points with no open question, in order;
    `slots`: how many questions may still be created under the cap."""
    to_close: Tuple[FactReviewQuestion, ...]
    to_queue: Tuple[OpenPoint, ...]
    slots: int


def plan_profile_questions(
    points: Sequence[OpenPoint], open_questions: Sequence[FactReviewQuestion], cap: int = PROFILE_OPEN_CAP,
) -> ProfileQuestionPlan:
    """The plan for `points` against the open profile questions; a question that is not a profile question is ignored."""
    wanted = {point.point.key for point in points}
    profile = [q for q in open_questions if q.is_profile and q.open_point is not None]
    to_close = tuple(q for q in profile if q.open_point is not None and q.open_point.key not in wanted)
    kept: Dict[str, FactReviewQuestion] = {
        q.open_point.key: q for q in profile if q.open_point is not None and q.open_point.key in wanted}
    to_queue: List[OpenPoint] = [point for point in points if point.point.key not in kept]
    return ProfileQuestionPlan(to_close, tuple(to_queue), max(0, cap - len(kept)))


def profile_question(point: OpenPoint, account_id: str, user_id: str, at: datetime) -> FactReviewQuestion:
    """The queued question of `point`."""
    return FactReviewQuestion(
        question_id=profile_question_id(account_id, point.point.key),
        account_id=account_id,
        user_id=user_id,
        fact_id=point.fact_id,
        quoted_text=point.quoted_text,
        fallback="none",
        queue_key=at,
        queued_at=at,
        question=_KIND_OF_FIELD[point.point.field],
        origin="profile",
        open_point=point.point,
    )
