"""The profile question on the review record (fact profile RFC §4.7): the validator ties kind, origin, open point, fallback
and texts together both ways, a lifecycle question stays as it was, an owner close of a profile question carries no answer
kind, and its words are never recorded on it. Invented cast."""
import pytest

from src.domain.exceptions import QuestionTransitionError
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import PROFILE_ANSWER_SOURCES, ClosedBy, FactReviewQuestion, QuestionStatus
from src.domain.profile_open_point import OpenPointOption, ProfileOpenPoint
from tests.support.profile_question_builders import LINEAGE, NOW, acme_point, doe_point, profile_question, rome_point
from tests.support.review_rule_builders import question as lifecycle_question

BASE = dict(question_id="pq1", account_id="acc", user_id="u1", fact_id="f1", quoted_text="Alex has a trip to Rome",
            queue_key=NOW, queued_at=NOW)


def build(**over):
    fields = {**BASE, "fallback": "none", "origin": "profile", "question": "place", "open_point": rome_point()}
    fields.update(over)
    return FactReviewQuestion(**fields)


# ------------------------------------------------------------------------------------------------ the validator

@pytest.mark.parametrize("kind", ["place", "person", "date"])
def test_each_profile_kind_builds_with_its_open_point_and_no_texts(kind):
    question = profile_question(kind)
    assert question.is_profile and question.open_point is not None and question.texts == {}
    assert question.fallback == "none" and question.origin == "profile" and question.delivery == "chat"


@pytest.mark.parametrize("over", [
    dict(origin="review"), dict(origin="kept_twice"), dict(open_point=None), dict(fallback="revive"),
    dict(fallback="history"), dict(texts={"paid": "x"}), dict(delivery="digest"),
])
def test_a_profile_kind_with_anything_but_its_profile_shape_is_refused(over):
    with pytest.raises(ValueError):
        build(**over)


@pytest.mark.parametrize("kind, point", [("place", doe_point()), ("person", rome_point()), ("date", rome_point()),
                                         ("place", acme_point())])
def test_the_open_point_field_must_be_the_kinds(kind, point):
    with pytest.raises(ValueError, match="open point of the field"):
        build(question=kind, open_point=point)


@pytest.mark.parametrize("over", [
    dict(question="still_true", fallback="revive"), dict(question="happened", fallback="history"),
])
def test_a_lifecycle_question_never_has_none_an_open_point_or_the_profile_origin(over):
    base = dict(BASE, origin="review", texts={"happened": "a", "not_happened": "b", "unknown": "c"}
                if over["question"] == "happened" else {})
    assert FactReviewQuestion(**{**base, **over}).is_profile is False
    for extra in (dict(fallback="none"), dict(open_point=rome_point()), dict(origin="profile")):
        with pytest.raises(ValueError):
            FactReviewQuestion(**{**base, **over, **extra})


def test_a_lifecycle_question_from_the_review_builders_still_validates():
    question = lifecycle_question()
    assert question.is_profile is False and question.open_point is None and question.fallback == "revive"


def test_the_stored_document_without_an_open_point_reads_as_a_lifecycle_question():
    assert lifecycle_question().model_dump()["open_point"] is None


# ------------------------------------------------------------------------------------------------ the open point

def test_an_open_point_needs_what_its_field_needs():
    with pytest.raises(ValueError, match="its city"):
        ProfileOpenPoint(key="k", field="places", printed="x")
    with pytest.raises(ValueError, match="lineage_id and mention"):
        ProfileOpenPoint(key="k", field="people", printed="x", mention="Doe")
    with pytest.raises(ValueError, match="no options"):
        ProfileOpenPoint(key="k", field="time_points", printed="x", lineage_id=LINEAGE, mention="x",
                         options=(OpenPointOption(id="pick1", label="a", value="b"),))


def test_the_candidate_options_are_three_at_most_and_numbered_in_order():
    four = tuple(OpenPointOption(id=f"pick{n}", label=f"Option {n}", value=str(n)) for n in range(1, 5))
    with pytest.raises(ValueError, match="at most 3"):
        ProfileOpenPoint(key="k", field="places", printed="x", city="Rome", options=four)
    with pytest.raises(ValueError, match="pick1, pick2, pick3"):
        ProfileOpenPoint(key="k", field="places", printed="x", city="Rome", options=(four[1],))


def test_an_option_label_fits_a_button():
    with pytest.raises(ValueError):
        OpenPointOption(id="pick1", label="x" * 76, value="1")


# ------------------------------------------------------------------------------------------------ no reply text

def test_a_profile_question_never_has_a_reply_text():
    with pytest.raises(ValueError, match="never has a reply_text"):
        profile_question("place", QuestionStatus.ASKED, reply_text="Rome, Italy", reply_at=NOW)


def test_with_reply_is_refused_for_a_profile_question_and_still_works_for_a_lifecycle_one():
    asked = profile_question("place", QuestionStatus.ASKED)
    with pytest.raises(QuestionTransitionError, match="profile question"):
        asked.with_reply("Rome, Italy", NOW)
    assert lifecycle_question(QuestionStatus.ASKED).with_reply("still true", NOW).reply_text == "still true"


# ------------------------------------------------------------------------------------------------ the owner close

@pytest.mark.parametrize("source", sorted(PROFILE_ANSWER_SOURCES))
def test_an_owner_close_of_a_profile_question_has_no_answer_and_a_digit_button_or_words_source(source):
    closed = profile_question("place", QuestionStatus.ASKED).closed(ClosedBy.OWNER, NOW, None, source)
    assert (closed.status, closed.answer, closed.answer_source) == (QuestionStatus.CLOSED, None, source)


def test_an_owner_close_of_a_profile_question_with_an_answer_or_another_source_is_refused():
    asked = profile_question("place", QuestionStatus.ASKED)
    with pytest.raises(QuestionTransitionError):
        asked.closed(ClosedBy.OWNER, NOW, AnswerKind.DONT_KNOW, "digit")
    with pytest.raises(QuestionTransitionError):
        asked.closed(ClosedBy.OWNER, NOW, None, "cabinet")
    with pytest.raises(QuestionTransitionError):
        asked.closed(ClosedBy.OWNER, NOW, None, None)


def test_an_owner_close_of_a_profile_question_needs_the_message_ref():
    fields = profile_question("place", QuestionStatus.ASKED).model_dump()
    fields.update(message_ref=None, status=QuestionStatus.PENDING, asked_at=None)
    pending = FactReviewQuestion.model_validate(fields)
    with pytest.raises(QuestionTransitionError, match="message_ref"):
        pending.closed(ClosedBy.OWNER, NOW, None, "digit")


def test_a_lifecycle_owner_close_is_as_before_and_refuses_the_words_source():
    asked = lifecycle_question(QuestionStatus.ASKED)
    assert asked.closed(ClosedBy.OWNER, NOW, AnswerKind.STILL_TRUE, "button").answer is AnswerKind.STILL_TRUE
    with pytest.raises(QuestionTransitionError):
        asked.closed(ClosedBy.OWNER, NOW, AnswerKind.STILL_TRUE, "words")


@pytest.mark.parametrize("by", [ClosedBy.TIMEOUT, ClosedBy.RESOLVED_ELSEWHERE])
def test_a_timeout_or_elsewhere_close_of_a_profile_question_carries_nothing(by):
    closed = profile_question("person", QuestionStatus.ASKED).closed(by, NOW)
    assert closed.answer is None and closed.answer_source is None and closed.is_profile

