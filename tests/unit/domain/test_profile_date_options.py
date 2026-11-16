"""The date question offers its candidate dates and a date answer replaces them (fact profile RFC §4.7): the candidate
rule, the options of the open point, the pick, the message and the reader's range forms. Invented cast: the Rome trip
in August 2025 and the Hamburg conference."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.domain.fact_profile import ProfileReviewItem, ProfileTimePoint
from src.domain.fact_profile_derive import apply_answers, date_candidates
from src.domain.fact_review_language import CANONICAL_LABELS, fact_question_message
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import EMPTY_ANSWERS, DateAnswer, date_point_key
from src.domain.profile_open_point import OpenPointOption, ProfileOpenPoint
from src.domain.profile_open_points import PointFact, open_points
from src.domain.profile_option_answer import profile_option_answer
from src.domain.profile_reply_words import read_date_reply
from src.domain.ui_messages import UIMessage
from src.services.fact_profile_service import FactProfileService
from tests.support.fact_kit import make_fact
from tests.support.profile_question_builders import LINEAGE, NOW, profile_question

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
MENTION = "Rome trip"
TRIP = ProfileTimePoint("2025-08-10", "2025-08-14", "day", "Rome trip")
MONTH = ProfileTimePoint("2025-08", "", "month", "hotel Albergo Immaginario")
CONF = ProfileTimePoint("2026-10-15", "", "day", "Hamburg conference")
EN = dict(CANONICAL_LABELS)


def point(options=()) -> ProfileOpenPoint:
    return ProfileOpenPoint(key=date_point_key(LINEAGE), field="time_points", printed=MENTION, lineage_id=LINEAGE,
                            mention=MENTION, options=tuple(options))


def date_fact(points, n=1) -> PointFact:
    return PointFact(f"f{n}", LINEAGE, "Alex has a trip to Rome", AT, (), (ProfileReviewItem("time_points", MENTION),),
                     tuple(points))


# ---- the candidate rule

def test_candidates_are_the_points_about_the_mention_and_the_points_overlapping_them_in_profile_order():
    assert date_candidates((CONF, MONTH, TRIP), MENTION) == (MONTH, TRIP)


def test_no_point_about_the_mention_gives_no_candidate():
    assert date_candidates((MONTH, CONF), MENTION) == ()


@pytest.mark.parametrize("other", [
    ProfileTimePoint("2025", "", "year", "metadata"),
    ProfileTimePoint("2025-08-14", "", "day", "metadata"),
    ProfileTimePoint("2025-08-09", "2025-08-10", "day", "metadata"),
    ProfileTimePoint("2025-08-01", "2025-08-31", "day", "metadata"),
])
def test_a_point_touching_the_span_is_a_candidate_ends_included(other):
    assert other in date_candidates((TRIP, other), MENTION)


@pytest.mark.parametrize("other", [
    ProfileTimePoint("2025-07", "", "month", "metadata"),
    ProfileTimePoint("2025-08-15", "", "day", "metadata"),
    ProfileTimePoint("2024", "", "year", "metadata"),
    ProfileTimePoint("not a date", "", "day", "metadata"),
    ProfileTimePoint("2025-13", "", "month", "metadata"),
    ProfileTimePoint("2025-08", "", "year", "metadata"),
])
def test_a_point_outside_or_unreadable_is_no_candidate(other):
    assert date_candidates((TRIP, other), MENTION) == (TRIP,)


def test_an_unreadable_point_about_the_mention_overlaps_nothing():
    broken = ProfileTimePoint("soon", "", "day", MENTION)
    assert date_candidates((broken, MONTH), MENTION) == (broken,)


# ---- the answer replaces the candidates

def test_a_date_answer_replaces_the_candidates_and_keeps_a_point_that_overlaps_nothing():
    answer = DateAnswer(key=date_point_key(LINEAGE), lineage_id=LINEAGE, mention=MENTION, resolution="date",
                        start="2025-08-10", end="2025-08-14", precision="day", question_id="q", answered_at=AT)
    out, _, flag, items = apply_answers(
        EMPTY_ANSWERS.with_answer(answer), LINEAGE, (MONTH, CONF, TRIP), (), True,
        (ProfileReviewItem("time_points", MENTION),), set(), (), {})
    assert out == (CONF, ProfileTimePoint("2025-08-10", "2025-08-14", "day", MENTION))
    assert (flag, items) == (False, ())


# ---- the options of the open point

def test_two_conflicting_candidates_are_two_options_in_iso_form():
    found = open_points([date_fact((MONTH, TRIP, CONF))], EMPTY_ANSWERS, None, PlaceNames([]))
    assert [(o.id, o.label, o.value) for o in found[0].point.options] == [
        ("pick1", "2025-08", "2025-08"), ("pick2", "2025-08-10 – 2025-08-14", "2025-08-10 – 2025-08-14")]


def test_equal_candidates_collapse_and_at_most_three_are_offered():
    same = ProfileTimePoint("2025-08", "", "month", "other words")
    many = [TRIP, same, MONTH,
            ProfileTimePoint("2025-08-10", "", "day", "a"), ProfileTimePoint("2025-08-11", "", "day", "b"),
            ProfileTimePoint("2025-08-12", "", "day", "c")]
    options = open_points([date_fact(many)], EMPTY_ANSWERS, None, PlaceNames([]))[0].point.options
    assert [o.value for o in options] == ["2025-08-10 – 2025-08-14", "2025-08", "2025-08-10"]


def test_no_candidate_means_no_options():
    assert open_points([date_fact((CONF,))], EMPTY_ANSWERS, None, PlaceNames([]))[0].point.options == ()


def test_every_option_value_reads_back_as_the_candidate():
    options = open_points([date_fact((MONTH, TRIP))], EMPTY_ANSWERS, None, PlaceNames([]))[0].point.options
    for option, candidate in zip(options, (MONTH, TRIP)):
        read = read_date_reply(option.value, point(), "q", AT)
        assert read is not None and (read.start, read.end, read.precision) == (
            candidate.start, candidate.end, candidate.precision)


# ---- picking an option

def date_question():
    options = (OpenPointOption(id="pick1", label="2025-08", value="2025-08"),
               OpenPointOption(id="pick2", label="2025-08-10 – 2025-08-14", value="2025-08-10 – 2025-08-14"))
    return profile_question("date", open_point=point(options))


def test_a_picked_date_is_a_date_answer_with_the_options_span():
    answer = profile_option_answer(date_question(), "pick2", NOW)
    assert isinstance(answer, DateAnswer) and answer.resolution == "date"
    assert (answer.start, answer.end, answer.precision) == ("2025-08-10", "2025-08-14", "day")
    assert (answer.key, answer.question_id, answer.answered_at) == (date_point_key(LINEAGE), "pq-date", NOW)


def test_an_unknown_pick_gives_nothing_and_dont_remember_still_works():
    assert profile_option_answer(date_question(), "pick3", NOW) is None
    unknown = profile_option_answer(date_question(), "dont_know", NOW)
    assert isinstance(unknown, DateAnswer) and unknown.resolution == "unknown"


# ---- the message

def test_a_date_question_shows_its_candidates_then_dont_remember_and_the_date_hint():
    shown = fact_question_message(date_question(), EN)
    assert [(o.id, o.label) for o in shown.options] == [
        ("pick1", "2025-08"), ("pick2", "2025-08-10 – 2025-08-14"), ("dont_know", "Don't remember")]
    assert shown.reply_hint == EN[UIMessage.MEMORY_PROFILE_DATE_REPLY_HINT.value]
    assert shown.reply_hint != EN[UIMessage.MEMORY_REPLY_HINT.value]


@pytest.mark.parametrize("kind", ["place", "person"])
def test_place_and_person_questions_keep_the_generic_hint(kind):
    assert fact_question_message(profile_question(kind), EN).reply_hint == EN[UIMessage.MEMORY_REPLY_HINT.value]


# ---- the reader

@pytest.mark.parametrize("text", [
    "2026-03-06-2026-03-08", "2026-03-06 - 2026-03-08", "2026-03-06 – 2026-03-08", "2026-03-06—2026-03-08",
    "2026-03-06 to 2026-03-08"])
def test_a_range_of_two_iso_values_is_read_whatever_joins_them(text):
    answer = read_date_reply(text, point(), "q", AT)
    assert answer is not None and (answer.start, answer.end, answer.precision) == ("2026-03-06", "2026-03-08", "day")


@pytest.mark.parametrize("text", ["2026-03-06-", "2026-03-06 to", "2026-03 - 2026-03-08", "2026-03-06-2026-03-08-2026",
                                  "2026-03-06to2026-03-08"])
def test_a_trailing_joiner_mixed_precision_or_a_third_value_is_refused(text):
    assert read_date_reply(text, point(), "q", AT) is None


def test_two_years_joined_by_a_bare_hyphen_are_a_range():
    answer = read_date_reply("2025-2026", point(), "q", AT)
    assert answer is not None and (answer.start, answer.end, answer.precision) == ("2025", "2026", "year")


# ---- the service passes the time points

def collect(fact, written=None):
    plan = SimpleNamespace(items=[SimpleNamespace(fact=fact)], answers=EMPTY_ANSWERS, confirmed=None,
                           names=PlaceNames([]))
    # (the profile's own review items are on the fact; the written tuple replaces them for a fact written this run)
    return FactProfileService._collect_points(plan, written or {})


def stored(p: ProfileTimePoint):
    return p.stored()


def test_a_fact_written_this_run_carries_the_time_points_it_was_written_with():
    fact = make_fact("Alex has a trip to Rome", time_points=[stored(CONF)])
    review = [{"field": "time_points", "mention": MENTION}]
    found = collect(fact, {fact.id: ([], review, [stored(TRIP), stored(MONTH)])})
    assert [o.value for o in found[0].point.options] == ["2025-08-10 – 2025-08-14", "2025-08"]


def test_any_other_fact_carries_its_stored_points_and_a_malformed_entry_is_skipped():
    fact = make_fact("Alex has a trip to Rome", profile_review_items=[{"field": "time_points", "mention": MENTION}],
                     time_points=[stored(TRIP), {"from": "2025-08"}, {**stored(CONF), "to": 5}])
    found = collect(fact)
    assert [o.value for o in found[0].point.options] == ["2025-08-10 – 2025-08-14"]


def test_a_fact_without_stored_points_gives_a_date_point_without_options():
    fact = make_fact("Alex has a trip to Rome", profile_review_items=[{"field": "time_points", "mention": MENTION}])
    assert collect(fact)[0].point.options == ()
