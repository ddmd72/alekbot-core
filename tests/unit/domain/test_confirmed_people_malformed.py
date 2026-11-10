"""A malformed stored confirmed-people document is refused (or read as empty) by the public parser alone."""
import pytest

from src.domain.confirmed_people import parse_confirmed_people

USER = {"name": "Alex Doe", "forms": ["Alex"]}
CLARA = {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"}


def test_a_non_string_name_is_refused():
    with pytest.raises(ValueError, match="user name must be a string"):
        parse_confirmed_people({"user": {"name": 42, "forms": []}, "people": []})


def test_forms_missing_means_no_forms():
    parsed = parse_confirmed_people({"user": {"name": "Alex Doe"}, "people": []})

    assert parsed.user.forms == ()


def test_forms_that_are_not_a_list_are_refused():
    with pytest.raises(ValueError, match="forms must be a list"):
        parse_confirmed_people({"user": {"name": "Alex Doe", "forms": "Alex"}, "people": []})


def test_a_non_string_form_is_refused():
    with pytest.raises(ValueError, match="form must be a string"):
        parse_confirmed_people({"user": {"name": "Alex Doe", "forms": [7]}, "people": []})


def test_an_entry_that_is_not_a_mapping_is_refused():
    with pytest.raises(ValueError, match="person 1 must be a mapping"):
        parse_confirmed_people({"user": USER, "people": ["Clara Moreira"]})


def test_people_missing_means_an_empty_list():
    parsed = parse_confirmed_people({"user": USER})

    assert parsed.people == () and parsed.entries() == (parsed.user,)


def test_people_that_are_not_a_list_are_refused():
    with pytest.raises(ValueError, match="people must be a list"):
        parse_confirmed_people({"user": USER, "people": CLARA})
