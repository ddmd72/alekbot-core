"""The model-facing code text of the card call (slice 3): the static block bytes, the placeholder render, the response
schema and the task line. Invented cast (tests/unit/domain/email_card_fixtures.py)."""
import re

import pytest

from src.domain.confirmed_people import parse_confirmed_people
from src.domain.drive_labels import DATE_LABELS, ENTITY_TYPES, LIFE_DOMAINS, VALUE_LABELS
from src.domain.email_card_answer import REQUIRED_FIELDS
from src.domain.email_card_labels import CARD_DOC_TYPES
from src.domain.email_card_people import person_handles
from src.domain.email_card_prompt import (
    STATIC_BLOCK_TITLE, card_placeholder_values, card_response_schema, card_task_line, render_card_prompt,
    render_card_static_block,
)
from tests.unit.domain.email_card_fixtures import PEOPLE

HEAD = (f"{STATIC_BLOCK_TITLE}\nThe owner: %s\nThe owner's mailbox: %s\n"
        "People the owner has confirmed, one per line as entry id | name | other forms | relation to the owner:\n%s")


def test_the_block_without_a_list():
    assert render_card_static_block(None, "") == HEAD % ("name not given", "not known", "none")


def test_the_block_with_the_fixture_list_is_pinned():
    assert render_card_static_block(PEOPLE, " alex@example.com ") == HEAD % (
        "Alex Doe; also written Alex", "alex@example.com",
        "P1 | Jamie Doe | Jamie | daughter\nP2 | Ana Doe | Ana | younger daughter\nP3 | Clara Moreira | Clara | partner")


def test_a_user_without_forms_and_a_person_without_forms():
    people = parse_confirmed_people({"user": {"name": "Alex Doe"}, "people": [
        {"name": "Maria Doe", "forms": [], "relation": "mother"}]})
    assert render_card_static_block(people, "alex@example.com") == HEAD % (
        "Alex Doe", "alex@example.com", "P1 | Maria Doe | none | mother")


def test_braces_in_a_name_cannot_look_like_a_placeholder():
    people = parse_confirmed_people({"user": {"name": "Alex {DOC_TYPES}"}, "people": [
        {"name": "Jamie {Doe}", "forms": ["{J}"], "relation": "daughter"}]})
    block = render_card_static_block(people, "a{b}@example.com")
    assert "{" not in block and "}" not in block
    assert "Alex (DOC_TYPES)" in block and "P1 | Jamie (Doe) | (J) | daughter" in block


def test_handle_order_equals_person_handles():
    lines = render_card_static_block(PEOPLE, "alex@example.com").splitlines()[-3:]
    assert [ln.split(" | ")[0] for ln in lines] == list(person_handles(PEOPLE))
    assert [ln.split(" | ")[1] for ln in lines] == [p.name for p in person_handles(PEOPLE).values()]


def test_the_seven_placeholders_render_once_from_the_code_lists():
    values = card_placeholder_values()
    assert set(values) == {"DOC_TYPES", "LIFE_DOMAINS", "ABOUT_VALUES", "DATE_LABELS", "ENTITY_TYPES", "VALUE_LABELS",
                           "SUBSTANCE_VALUES"}
    text = "doc_type: one of {DOC_TYPES}. about: {ABOUT_VALUES}. substance: {SUBSTANCE_VALUES}. cognitive_process {\n}"
    out = render_card_prompt(text)
    assert values["DOC_TYPES"] in out and "owner, household, person, unknown" in out and "some, none" in out
    assert "cognitive_process {" in out and not re.search(r"\{[A-Z_]+\}", out)


def test_an_unknown_placeholder_is_named():
    with pytest.raises(ValueError) as err:
        render_card_prompt("{DOC_TYPES} {FOO_BAR} {FOO_BAR} {BAZ}")
    assert "FOO_BAR" in str(err.value) and "BAZ" in str(err.value) and "DOC_TYPES" not in str(err.value)


def test_a_value_is_never_scanned_again():
    assert render_card_prompt("{VALUE_LABELS}") == ", ".join(VALUE_LABELS)


def test_the_schema_enums_are_the_code_lists_and_required_is_the_answers():
    schema = card_response_schema()
    props = schema["properties"]
    assert schema["required"] == list(REQUIRED_FIELDS) and schema["propertyOrdering"] == list(REQUIRED_FIELDS)
    assert list(props) == list(REQUIRED_FIELDS)
    assert props["doc_type"]["enum"] == list(CARD_DOC_TYPES)
    assert props["life_domains"]["items"]["enum"] == list(LIFE_DOMAINS)
    assert props["about"]["enum"] == ["owner", "household", "person", "unknown"]
    assert props["dates"]["items"]["properties"]["label"]["enum"] == list(DATE_LABELS)
    assert props["entities"]["items"]["properties"]["type"]["enum"] == list(ENTITY_TYPES)
    assert props["key_values"]["items"]["properties"]["label"]["enum"] == list(VALUE_LABELS)
    assert props["substance"]["enum"] == ["some", "none"]
    assert not re.search(r"\{[A-Z_]+\}", str(schema))


def test_the_task_line_states_the_caps_in_digits():
    assert card_task_line() == (
        "Write the card of this mail as the output format says. Limits: at most 12 entities with at most 3 forms each, "
        "12 key values, 5 dates, 6 people, 8 topics and 8 native topics, and 12 words in each word list.")
