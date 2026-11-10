"""The owner-confirmed people list (fact profile RFC §4.5): parsing rules, the document shape, which entries a text names
and the exact bytes of the block the profiler reads. The list is the invented universe's household."""
import pytest

from src.domain.confirmed_people import (
    PEOPLE_BLOCK_TITLE, USER_RELATION, ConfirmedPeople, ConfirmedPerson, confirmed_people_document,
    parse_confirmed_people, people_named_in_texts, render_people_block,
)

RAW = {
    "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
    "people": [
        {"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
        {"name": "Jamie Doe", "forms": ["Jamie"], "relation": "older daughter"},
        {"name": "Ana Doe", "forms": [], "relation": "younger daughter"},
        {"name": "Maria Doe", "forms": ["Maria A. Doe", "Mariia", "Mia"], "relation": "mother"},
        {"name": "Hanna Doe", "forms": ["Hanna"], "relation": "sister"},
    ],
}


@pytest.fixture
def household():
    return parse_confirmed_people(RAW)


def names(entries):
    return [e.name for e in entries]


# ------------------------------------------------------------------------------------------------ parsing

def test_a_valid_list_keeps_the_owners_order_with_the_user_first(household):
    assert household.user == ConfirmedPerson("Alex Doe", ("Alex", "Bo"), "", True)
    assert names(household.entries()) == ["Alex Doe", "Clara Moreira", "Jamie Doe", "Ana Doe", "Maria Doe", "Hanna Doe"]


def test_the_document_round_trips(household):
    assert parse_confirmed_people(confirmed_people_document(household)) == household
    assert confirmed_people_document(household) == RAW


def test_whitespace_is_collapsed_and_forms_are_deduplicated_without_the_name_itself():
    parsed = parse_confirmed_people({
        "user": {"name": "  Alex   Doe ", "forms": ["alex doe", "Alex", "ALEX", " Bo "]},
        "people": [{"name": "Maria Doe", "forms": ["Mia", "mia", "Maria  Doe"], "relation": "mother"}]})
    assert parsed.user.name == "Alex Doe" and parsed.user.forms == ("Alex", "Bo")
    assert parsed.people[0].forms == ("Mia",)


@pytest.mark.parametrize("change, why", [
    (lambda d: d["user"].update(name=""), "user without a name"),
    (lambda d: d["people"][0].update(name=" "), "person without a name"),
    (lambda d: d["people"][0].update(relation=""), "person without a relation"),
    (lambda d: d["people"][0].update(relation="partner 2"), "digits in the relation"),
    (lambda d: d["people"][0].update(relation="Partner"), "relation not lowercase"),
    (lambda d: d["people"][0].update(forms=["Clara", ""]), "an empty form"),
    (lambda d: d["people"][1].update(name="clara moreira"), "the same name twice"),
    (lambda d: d["people"][1].update(name="ALEX DOE"), "a person named like the user"),
])
def test_a_broken_rule_raises(change, why):
    data = {"user": dict(RAW["user"]), "people": [dict(p) for p in RAW["people"]]}
    change(data)
    with pytest.raises(ValueError):
        parse_confirmed_people(data)


def test_a_list_that_is_not_a_mapping_raises():
    with pytest.raises(ValueError):
        parse_confirmed_people(["Alex"])  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ selection

def test_a_first_name_selects_that_entry_only(household):
    assert names(people_named_in_texts(household, ["Jamie has a parents' meeting on March 3, 2026."])) == ["Jamie Doe"]


def test_a_shared_surname_selects_every_entry_carrying_it(household):
    assert names(people_named_in_texts(household, ["Christmas lunch at the Doe home."])) == [
        "Alex Doe", "Jamie Doe", "Ana Doe", "Maria Doe", "Hanna Doe"]


def test_the_transliteration_in_the_other_script_selects_by_skeleton(household):
    assert names(people_named_in_texts(household, ["Марія любить чай."])) == [
        "Maria Doe"]


def test_a_form_of_the_user_selects_the_user(household):
    assert names(people_named_in_texts(household, ["Bo had a lab test on 14/03/2025."])) == ["Alex Doe"]


def test_a_relation_word_selects_the_entries_with_that_relation_in_any_inflection(household):
    assert names(people_named_in_texts(household, ["The user's mother likes tea."])) == ["Maria Doe"]
    assert names(people_named_in_texts(household, ["Both daughters go to Example School."])) == [
        "Jamie Doe", "Ana Doe"]
    assert names(people_named_in_texts(household, ["The sister's lease in Porto."])) == ["Hanna Doe"]


def test_the_users_relation_is_not_a_word_to_look_for(household):
    assert people_named_in_texts(household, ["The user drank green tea until Oct 2026."]) == ()


def test_no_listed_word_selects_nothing_and_no_list_selects_nothing(household):
    assert people_named_in_texts(household, ["Robin goes to the Iron Temple gym."]) == ()
    assert people_named_in_texts(None, ["Jamie"]) == ()


def test_selection_is_in_list_order_whatever_the_order_in_the_text(household):
    assert names(people_named_in_texts(household, ["Contacts: Hanna, Clara, Alex."])) == [
        "Alex Doe", "Clara Moreira", "Hanna Doe"]


def test_any_of_the_texts_counts_so_a_metadata_value_names_a_person(household):
    assert names(people_named_in_texts(household, ["Dentist check-up on 2026-10-12", "patient: Bo"])) == ["Alex Doe"]


# ------------------------------------------------------------------------------------------------ rendering

def test_the_block_bytes_for_the_whole_household(household):
    assert render_people_block(household.entries()) == (
        "Confirmed people (the first entry is the user):\n"
        "- Alex Doe (also written: Alex, Bo) — the user\n"
        "- Clara Moreira (also written: Clara) — partner\n"
        "- Jamie Doe (also written: Jamie) — older daughter\n"
        "- Ana Doe — younger daughter\n"
        "- Maria Doe (also written: Maria A. Doe, Mariia, Mia) — mother\n"
        "- Hanna Doe (also written: Hanna) — sister\n"
        "\n")


def test_no_entries_render_nothing():
    assert render_people_block(()) == ""


def test_the_title_and_the_user_relation_are_the_constants(household):
    block = render_people_block(household.entries()[:1])
    assert block.splitlines()[0] == PEOPLE_BLOCK_TITLE
    assert block.splitlines()[1].endswith(f"— {USER_RELATION}")
    assert isinstance(household, ConfirmedPeople)
