"""The open points the daily review may ask about (fact profile RFC §4.7): which unresolved places, flagged people and
dates become a point, how candidates are labelled and in what order points come. Invented cast and places only."""
from datetime import datetime, timedelta, timezone

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.fact_profile import ProfileReviewItem, UnresolvedPlace
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import (
    EMPTY_ANSWERS, PersonAnswer, PlaceAnswer, date_point_key, person_point_key, place_point_key,
)
from src.domain.profile_open_points import PointFact, open_points

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
ROME_US = PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000")
ROME_IT = PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000")
PORTO = PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",), geonameid="3000")
NAMES = PlaceNames([ROME_US, ROME_IT, PORTO])
PEOPLE = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [{"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"},
               {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"}]})
ROME_FACT = "Alex has a trip to Rome"


def fact(n: int, text: str = ROME_FACT, *, places=(), items=(), lineage: str = "") -> PointFact:
    return PointFact(f"f{n}", lineage or f"lin{n}", text, T0 + timedelta(hours=n),
                     tuple(UnresolvedPlace(c, k) for c, k in places), tuple(ProfileReviewItem(f, m) for f, m in items))


def points(view, answers=EMPTY_ANSWERS):
    return open_points(view, answers, PEOPLE, NAMES)


def test_three_facts_printing_rome_without_a_country_are_one_point_with_the_newest_quoted():
    view = [fact(1, places=[("Rome", "")]), fact(3, "Alex has a trip to Rome again", places=[("Rome", "")]),
            fact(2, places=[("rome", "")])]

    found = points(view)

    assert len(found) == 1
    point = found[0]
    assert (point.facts, point.fact_id, point.quoted_text) == (3, "f3", "Alex has a trip to Rome again")
    assert point.newest == T0 + timedelta(hours=3)
    assert point.point.key == place_point_key("Rome", "") and point.point.printed == "Rome"
    assert point.point.reason == "ambiguous"
    assert [(o.id, o.label, o.value) for o in point.point.options] == [
        ("pick1", "Rome, Italy", "1000"), ("pick2", "Rome, United States", "2000")]


def test_a_place_the_atlas_resolves_is_no_point():
    assert points([fact(1, "Hanna lives in Porto", places=[("Porto", "Portugal")])]) == []


def test_a_place_with_an_answer_is_no_point_unknown_included():
    key = place_point_key("Rome", "")
    for resolution in ("atlas", "local", "unknown"):
        answer = PlaceAnswer(key=key, city="Rome", country="", resolution=resolution,
                             geonameid="1000" if resolution == "atlas" else "", question_id="pq1", answered_at=T0)
        assert points([fact(1, places=[("Rome", "")])], EMPTY_ANSWERS.with_answer(answer)) == []


def test_a_place_the_atlas_lacks_is_a_point_with_no_candidates_and_prints_its_country():
    found = points([fact(1, "Doe has a trip to Lisbon", places=[("Lisbon", "Portugal")])])

    assert len(found) == 1
    assert found[0].point.printed == "Lisbon, Portugal" and found[0].point.reason == "not_in_atlas"
    assert found[0].point.options == ()


def test_the_key_folds_the_city_and_the_country():
    lacking = [fact(1, "Doe has a trip to Lisbon", places=[("Lisbon", "Portugal")]),
               fact(2, "Doe has a trip to Lisbon", places=[("lisbon", "PORTUGAL")])]
    assert [(p.facts, p.point.key) for p in points(lacking)] == [(2, place_point_key("Lisbon", "Portugal"))]


def test_equal_labels_take_the_large_neighbour_and_still_equal_labels_keep_the_heavier_row():
    big = PlaceRow(5.0, 5.0, "Porto", "Porto", "Portugal", 5000, country_names=("Portugal",), geonameid="5000")
    near_a = PlaceRow(6.0, 6.0, "Rome", "Roma", "Italy", 900, country_names=("Italy",), large=big, geonameid="6000")
    near_b = PlaceRow(7.0, 7.0, "Rome", "Roma", "Italy", 800, country_names=("Italy",), large=PORTO, geonameid="7000")
    twin = PlaceRow(8.0, 8.0, "Rome", "Roma", "Italy", 100, country_names=("Italy",), geonameid="8000")
    names = PlaceNames([near_a, near_b, twin])

    found = open_points([fact(1, places=[("Rome", "")])], EMPTY_ANSWERS, PEOPLE, names)

    assert [(o.id, o.label, o.value) for o in found[0].point.options] == [
        ("pick1", "Rome, Italy (near Porto)", "6000"), ("pick2", "Rome, Italy", "8000")]


def test_a_person_mention_that_fits_two_entries_is_asked_with_both_and_the_user_by_name_only():
    found = points([fact(1, "Doe has a trip to Lisbon", lineage="lin-doe", items=[("people", "Doe")])])

    assert len(found) == 1
    point = found[0]
    assert point.point.key == person_point_key("lin-doe", "Doe") and point.facts == 1
    assert [(o.label, o.value) for o in point.point.options] == [
        ("Alex Doe", "Alex Doe"), ("Jamie Doe — daughter", "Jamie Doe")]


def test_a_person_mention_fitting_nobody_or_already_answered_is_no_point():
    assert points([fact(1, lineage="lin-1", items=[("people", "Hanna")])]) == []
    answer = PersonAnswer(key=person_point_key("lin-1", "Doe"), lineage_id="lin-1", mention="Doe",
                          resolution="unknown", question_id="pq1", answered_at=T0)
    assert points([fact(1, lineage="lin-1", items=[("people", "Doe")])], EMPTY_ANSWERS.with_answer(answer)) == []


def test_a_date_item_is_one_point_per_lineage_with_no_options():
    view = [fact(1, "Alex starts at Acme on 2026-10-01", lineage="lin-acme",
                 items=[("time_points", "on 2026-10-01"), ("time_points", "2026")])]

    found = points(view)

    assert len(found) == 1
    assert found[0].point.key == date_point_key("lin-acme") and found[0].point.printed == "on 2026-10-01"
    assert found[0].point.options == () and found[0].point.field == "time_points"


def test_places_items_and_flags_without_a_matching_place_are_not_asked():
    assert points([fact(1, items=[("places", "Rome")])]) == []


def test_points_come_by_the_number_of_facts_then_the_newest_fact_then_the_key():
    rome = [fact(1, places=[("Rome", "")]), fact(2, places=[("Rome", "")])]
    lisbon = fact(5, "Doe has a trip to Lisbon", places=[("Lisbon", "Portugal")])
    acme = fact(4, "Alex starts at Acme on 2026-10-01", lineage="lin-acme", items=[("time_points", "on 2026-10-01")])
    doe = fact(3, "Doe has a trip to Lisbon", lineage="lin-doe", items=[("people", "Doe")])

    found = points([*rome, doe, acme, lisbon])

    assert [(p.facts, p.point.printed) for p in found] == [
        (2, "Rome"), (1, "Lisbon, Portugal"), (1, "on 2026-10-01"), (1, "Doe")]


def test_points_of_one_count_and_one_fact_come_by_key():
    both = fact(1, "Doe has a trip to Lisbon", lineage="lin-doe", places=[("Lisbon", "Portugal")],
                items=[("people", "Doe")])

    assert [p.point.field for p in points([both])] == ["people", "places"]
