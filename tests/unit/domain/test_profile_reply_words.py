"""The owner's words read by code (fact profile RFC §4.7 "Words"): the forms read and the replies refused. Invented cast
and places only; the date forms are the ones the derivation reads (`owner_time_point`)."""
from datetime import datetime, timezone

import pytest

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import date_point_key, person_point_key, place_point_key
from src.domain.profile_open_point import OpenPointOption, ProfileOpenPoint
from src.domain.profile_reply_words import PersonReading, PlaceReading, read_date_reply, read_person_reply, read_place_reply

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
ROME_US = PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000")
ROME_IT = PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000")
PORTO = PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",), geonameid="3000")
NAMES = PlaceNames([ROME_US, ROME_IT, PORTO])
ROME = ProfileOpenPoint(
    key=place_point_key("Rome", ""), field="places", printed="Rome", city="Rome", reason="ambiguous",
    options=(OpenPointOption(id="pick1", label="Rome, Italy", value="1000"),
             OpenPointOption(id="pick2", label="Rome, United States", value="2000")))
PEOPLE = parse_confirmed_people({
    "user": {"name": "Alex Doe", "forms": ["Alex"]},
    "people": [{"name": "Jamie Doe", "forms": ["Jamie"], "relation": "daughter"},
               {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"}]})
DOE = ProfileOpenPoint(
    key=person_point_key("lin-1", "Doe"), field="people", printed="Doe", lineage_id="lin-1", mention="Doe",
    options=(OpenPointOption(id="pick1", label="Alex Doe", value="Alex Doe"),
             OpenPointOption(id="pick2", label="Jamie Doe — daughter", value="Jamie Doe")))
ACME = ProfileOpenPoint(key=date_point_key("lin-1"), field="time_points", printed="on 2026-10-01", lineage_id="lin-1",
                        mention="on 2026-10-01")


def place(text: str, point: ProfileOpenPoint = ROME, names: PlaceNames = NAMES):
    return read_place_reply(text, point, names)


# ------------------------------------------------------------------------------------------------ places

def test_a_city_and_a_country_the_atlas_places_in_one_row_is_that_row():
    assert place("Rome, Italy") == PlaceReading("atlas", "Rome", "Italy", "1000")
    assert place("rome , italia") == PlaceReading("atlas", "rome", "Italy", "1000")


def test_a_city_the_atlas_lacks_in_a_known_country_is_kept_as_typed_with_the_atlas_country_name():
    assert place("Porto, Italy") == PlaceReading("local", "Porto", "Italy")
    assert place("Rome, Portugal") == PlaceReading("local", "Rome", "Portugal")


def test_the_city_is_what_precedes_the_last_comma():
    assert place("Rome, Italy, Italy") is None
    assert place("Rome, Italy,") is None


@pytest.mark.parametrize("text", ["Rome, Atlantis", ", Italy", "Rome,", ""])
def test_an_unknown_country_or_a_missing_part_is_not_read(text):
    assert place(text) is None


def test_a_city_the_atlas_cannot_pick_between_inside_the_country_is_not_read():
    twin = PlaceRow(9.0, 9.0, "Rome", "Roma", "Italy", 50, country_names=("Italy",), geonameid="9000")
    assert place("Rome, Italy", names=PlaceNames([ROME_IT, twin])) is None


def test_a_country_that_names_exactly_one_shown_option_is_that_row():
    assert place("Italy") == PlaceReading("atlas", "Rome", "Italy", "1000")
    assert place("united states") == PlaceReading("atlas", "Rome", "United States", "2000")


def test_a_country_that_names_no_shown_option_is_not_read():
    assert place("Portugal") is None


def test_a_city_the_atlas_places_in_one_row_is_that_row_and_a_city_it_lacks_is_not_read():
    assert place("Porto") == PlaceReading("atlas", "Rome", "Portugal", "3000")
    assert place("Jamie") is None
    assert place("Rome") is None     # two rows


def test_the_question_country_narrows_a_bare_city():
    in_italy = ProfileOpenPoint(key=place_point_key("Rome", "Italy"), field="places", printed="Rome, Italy", city="Rome",
                                country="Italy", reason="ambiguous")
    assert place("Rome", in_italy) == PlaceReading("atlas", "Rome", "Italy", "1000")


@pytest.mark.parametrize("text", ["Rome, Italy 2026", "2026", "Rome\nItaly", "one two three four five six seven",
                                  "   "])
def test_a_digit_two_lines_more_than_six_words_or_blank_is_not_read_as_a_place(text):
    assert place(text) is None


def test_six_words_are_read():
    long_name = PlaceRow(5.0, 5.0, "Frankfurt am Main", "Frankfurt", "Italy", 10, country_names=("Italy",),
                         geonameid="5000")
    assert place("Frankfurt am Main, Italy", names=PlaceNames([long_name])) == PlaceReading(
        "atlas", "Frankfurt am Main", "Italy", "5000")


def test_a_place_reply_on_another_kind_of_point_is_not_read():
    assert place("Rome, Italy", DOE) is None


# ------------------------------------------------------------------------------------------------ people

def test_a_name_or_a_form_of_one_shown_entry_is_that_entry():
    assert read_person_reply("Jamie Doe", DOE, PEOPLE) == PersonReading("Jamie Doe")
    assert read_person_reply("jamie", DOE, PEOPLE) == PersonReading("Jamie Doe")
    assert read_person_reply("Alex", DOE, PEOPLE) == PersonReading("Alex Doe")


def test_an_entry_the_question_does_not_show_is_not_read():
    assert read_person_reply("Clara", DOE, PEOPLE) is None
    assert read_person_reply("Clara Moreira", DOE, PEOPLE) is None


@pytest.mark.parametrize("text", ["Doe", "Jamie 2026", "Jamie\nDoe", "", "Jamie Doe the daughter of Alex Doe"])
def test_a_shared_surname_a_digit_two_lines_or_too_many_words_is_not_read(text):
    assert read_person_reply(text, DOE, PEOPLE) is None


def test_without_a_list_or_on_another_kind_of_point_nothing_is_read():
    assert read_person_reply("Jamie", DOE, None) is None
    assert read_person_reply("Jamie", ROME, PEOPLE) is None


# ------------------------------------------------------------------------------------------------ dates

def date(text: str, point: ProfileOpenPoint = ACME):
    return read_date_reply(text, point, "pq1", AT)


@pytest.mark.parametrize("text, start, end, precision", [
    ("2026-10-01", "2026-10-01", "", "day"),
    ("2026-10", "2026-10", "", "month"),
    ("2026", "2026", "", "year"),
    ("2026-10-01 - 2026-12-01", "2026-10-01", "2026-12-01", "day"),
    ("2026-10-01 to 2026-12-01", "2026-10-01", "2026-12-01", "day"),
    ("2026–2027", "2026", "2027", "year"),
    (" 2026-10 — 2026-12 ", "2026-10", "2026-12", "month"),
])
def test_a_year_a_month_a_day_or_a_range_of_two_is_read_with_the_derivations_reader(text, start, end, precision):
    answer = date(text)

    assert answer is not None
    assert (answer.start, answer.end, answer.precision, answer.resolution) == (start, end, precision, "date")
    assert (answer.key, answer.lineage_id, answer.mention) == (ACME.key, "lin-1", "on 2026-10-01")
    assert (answer.question_id, answer.answered_at) == ("pq1", AT)


@pytest.mark.parametrize("text", [
    "", "Oct 2026", "2026-13", "2026-02-30", "26", "2026-10-01 - 2026", "2027-01-01 - 2026-10-01",
    "2026 - 2027 - 2028", "2026-10-01\n2026", "one two three four five six seven", "0001", "2026-10-1",
])
def test_anything_else_is_not_read(text):
    assert date(text) is None


def test_a_date_reply_on_another_kind_of_point_is_not_read():
    assert date("2026", ROME) is None
