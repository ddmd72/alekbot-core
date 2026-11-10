"""The answer each option of a profile question gives (fact profile RFC §4.7): candidates by their value, keep as written,
not on my list and don't remember, and None for an option the kind does not offer. Invented cast."""
import pytest

from src.domain.profile_answers import DateAnswer, PersonAnswer, PlaceAnswer
from src.domain.profile_option_answer import profile_option_answer
from tests.support.profile_question_builders import LINEAGE, NOW, porto_point, profile_question


def test_a_picked_place_row_is_an_atlas_answer_with_its_geonameid():
    answer = profile_option_answer(profile_question("place"), "pick2", NOW)
    assert isinstance(answer, PlaceAnswer)
    assert (answer.key, answer.city, answer.resolution, answer.geonameid) == ("place:rome|", "Rome", "atlas", "2000")
    assert (answer.question_id, answer.answered_at) == ("pq-place", NOW)


def test_keep_as_written_is_a_local_answer_with_the_city_and_country_of_the_point():
    question = profile_question("place", open_point=porto_point())
    answer = profile_option_answer(question, "keep", NOW)
    assert isinstance(answer, PlaceAnswer)
    assert (answer.resolution, answer.city, answer.country, answer.geonameid) == ("local", "Porto", "Portugal", "")


def test_dont_remember_is_an_unknown_answer_of_every_kind():
    place = profile_option_answer(profile_question("place"), "dont_know", NOW)
    person = profile_option_answer(profile_question("person"), "dont_know", NOW)
    date = profile_option_answer(profile_question("date"), "dont_know", NOW)
    assert isinstance(place, PlaceAnswer) and place.resolution == "unknown"
    assert isinstance(person, PersonAnswer) and person.resolution == "unknown" and person.name == ""
    assert isinstance(date, DateAnswer) and date.resolution == "unknown" and date.start == ""
    assert (date.key, date.lineage_id, date.mention) == (f"date:{LINEAGE}", LINEAGE, "on 2026-10-01")


def test_a_picked_list_entry_is_an_entry_answer_of_the_lineage_and_mention():
    answer = profile_option_answer(profile_question("person"), "pick2", NOW)
    assert isinstance(answer, PersonAnswer)
    assert (answer.resolution, answer.name, answer.lineage_id, answer.mention) == ("entry", "Jamie Doe", LINEAGE, "Doe")
    assert answer.key == f"person:{LINEAGE}:doe"


def test_not_on_my_list_is_a_not_listed_answer_without_a_name():
    answer = profile_option_answer(profile_question("person"), "not_listed", NOW)
    assert isinstance(answer, PersonAnswer) and answer.resolution == "not_listed" and answer.name == ""


@pytest.mark.parametrize("kind, option", [
    ("place", "not_listed"), ("place", "pick3"), ("place", "1"), ("place", ""), ("person", "keep"), ("person", "pick3"),
    ("date", "keep"), ("date", "pick1"), ("date", "not_listed"),
])
def test_an_option_the_kind_does_not_offer_gives_nothing(kind, option):
    assert profile_option_answer(profile_question(kind), option, NOW) is None
