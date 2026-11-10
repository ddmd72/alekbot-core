"""Which profile questions the daily review creates and closes (fact profile RFC §4.7, PR 5e): the points against the open
profile questions, the cap on open ones, and the queued record of a point. Invented cast."""
from datetime import timedelta

from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_rules import PROFILE_OPEN_CAP, profile_question_id
from src.domain.profile_open_points import OpenPoint
from src.domain.profile_question_plan import plan_profile_questions, profile_question
from tests.support.profile_question_builders import (
    ACME_FACT, DOE_FACT, NOW, PORTO_FACT, ROME_FACT, acme_point, doe_point, porto_point, profile_question as open_one,
    rome_point,
)
from tests.support.review_rule_builders import question as lifecycle_question

ROME = OpenPoint(rome_point(), "f-rome", ROME_FACT, 3, NOW)
PORTO = OpenPoint(porto_point(), "f-porto", PORTO_FACT, 2, NOW)
DOE = OpenPoint(doe_point(), "f-doe", DOE_FACT, 1, NOW)
ACME = OpenPoint(acme_point(), "f-acme", ACME_FACT, 1, NOW)


def asked(point: OpenPoint, kind: str):
    return open_one(kind, QuestionStatus.ASKED, question_id=profile_question_id("acc", point.point.key),
                    open_point=point.point)


def test_the_cap_is_three():
    assert PROFILE_OPEN_CAP == 3


def test_nothing_open_every_point_is_a_candidate_with_the_cap_as_slots():
    plan = plan_profile_questions([ROME, PORTO, DOE, ACME], [])

    assert plan.to_close == () and plan.to_queue == (ROME, PORTO, DOE, ACME) and plan.slots == 3


def test_a_point_with_an_open_question_is_not_a_candidate_and_takes_a_slot():
    plan = plan_profile_questions([ROME, PORTO, DOE], [asked(ROME, "place")])

    assert plan.to_queue == (PORTO, DOE) and plan.slots == 2 and plan.to_close == ()


def test_an_open_question_whose_point_is_gone_is_to_close_and_frees_its_slot():
    gone = asked(PORTO, "place")

    plan = plan_profile_questions([ROME], [gone, asked(ROME, "place")])

    assert plan.to_close == (gone,) and plan.to_queue == () and plan.slots == 2


def test_with_the_cap_used_no_slot_is_left():
    open_three = [asked(ROME, "place"), asked(PORTO, "place"), asked(DOE, "person")]

    plan = plan_profile_questions([ROME, PORTO, DOE, ACME], open_three)

    assert plan.slots == 0 and plan.to_queue == (ACME,)


def test_a_lifecycle_question_is_ignored():
    plan = plan_profile_questions([ROME], [lifecycle_question()])

    assert plan.to_close == () and plan.to_queue == (ROME,) and plan.slots == 3


def test_the_queued_question_of_a_point_carries_its_id_kind_quote_and_place_in_line():
    for point, kind in ((ROME, "place"), (DOE, "person"), (ACME, "date")):
        question = profile_question(point, "acc", "u1", NOW + timedelta(hours=1))

        assert question.question_id == profile_question_id("acc", point.point.key)
        assert (question.question, question.origin, question.fallback, question.status) == (
            kind, "profile", "none", QuestionStatus.QUEUED)
        assert (question.fact_id, question.quoted_text, question.open_point) == (
            point.fact_id, point.quoted_text, point.point)
        assert question.queue_key == question.queued_at == NOW + timedelta(hours=1)
        assert question.texts == {} and question.account_id == "acc" and question.user_id == "u1"
