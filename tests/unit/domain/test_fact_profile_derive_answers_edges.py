"""The remaining branches of the owner answers in the derivation (fact profile RFC §4.7): a point with no source, people
the answer does not name and a date answer with no room under the time cap. Invented cast; dates only 2026-10-01 and
the year 2026."""
from datetime import datetime, timezone

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.drive_keys import word_form_keys
from src.domain.fact_profile import ProfilePerson, ProfileReviewItem, ProfileTimePoint
from src.domain.fact_profile_derive import TIME_CAP, apply_answers, name_keys
from src.domain.profile_answers import (
    EMPTY_ANSWERS, DateAnswer, PersonAnswer, date_point_key, person_point_key,
)

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
LINEAGE = "lin-1"
LISTED = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [{"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"}]}).entries()


def dated(mention: str, start: str = "2026-10-01") -> DateAnswer:
    return DateAnswer(key=date_point_key(LINEAGE), lineage_id=LINEAGE, mention=mention, resolution="date",
                      start=start, precision="day", question_id="pq1", answered_at=AT)


def apply_date(points, answer: DateAnswer, mention: str):
    stored = EMPTY_ANSWERS.with_answer(answer)
    drops = {}
    out = apply_answers(stored, LINEAGE, points, (), True, (ProfileReviewItem("time_points", mention),), set(),
                        LISTED, drops)
    return out, drops


def test_a_point_without_a_readable_source_is_kept_by_a_date_answer_about_other_words():
    blank = ProfileTimePoint("2026", "", "year", "")
    (points, _, flag, items), drops = apply_date((blank,), dated("at Acme"), "at Acme")
    assert points == (blank, ProfileTimePoint("2026-10-01", "", "day", "at Acme"))
    assert (flag, items, drops) == (False, (), {"review_item_answered": 1})


def test_a_person_the_answer_does_not_name_stays_beside_the_one_it_does():
    other = ProfilePerson("Clara Moreira", "partner", name_keys("Clara Moreira"), word_form_keys("partner"))
    doe = ProfilePerson("Doe", "", name_keys("Doe"), ())
    stored = EMPTY_ANSWERS.with_answer(PersonAnswer(
        key=person_point_key(LINEAGE, "Doe"), lineage_id=LINEAGE, mention="Doe", resolution="entry",
        name="Jamie Doe", question_id="pq2", answered_at=AT))
    _, people, flag, items = apply_answers(stored, LINEAGE, (), (other, doe), True,
                                           (ProfileReviewItem("people", "Doe"),), set(), LISTED, {})
    assert [(p.name, p.role) for p in people] == [("Clara Moreira", "partner"), ("Doe", "daughter")]
    assert (flag, items) == (False, ())


def test_a_date_answer_with_no_room_left_under_the_cap_is_not_applied_and_is_counted():
    full = tuple(ProfileTimePoint(str(2000 + i), "", "year", f"year {2000 + i}") for i in range(TIME_CAP))
    (points, _, flag, items), drops = apply_date(full, dated("at Acme"), "at Acme")
    assert points == full and flag is True and items == (ProfileReviewItem("time_points", "at Acme"),)
    assert drops == {"date_answer_over_cap": 1}
