"""The code text of the email search v2 slots call (email index v2 §5.2; slice 8): the placeholder values are the sets the
slot parser checks, a leftover placeholder is refused, the schema's closed lists are the code's sets, the user message
carries the owner's Today in his zone, and `none` (or any other unknown intent) gives no time slot. Invented values only."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.domain.drive_labels import LIFE_DOMAINS
from src.domain.email_card_labels import CARD_DOC_TYPES
from src.domain.email_query_slots import (
    NO_TIME, SLOTS_PROFILE, render_slots_prompt, slot_doc_types, slot_domains, slots_placeholder_values,
    slots_response_schema, slots_retry_message, slots_user_message,
)
from src.domain.email_search_params import EXACT_KEYS_MAX, PEOPLE_SLOTS_MAX
from src.domain.email_search_query import SCOPES, TIME_INTENTS, parse_query_slots

LISBON = ZoneInfo("Europe/Lisbon")


def _listed(name: str):
    return slots_placeholder_values()[name].split(", ")


def test_the_profile_name():
    assert SLOTS_PROFILE == "email_query_slots"


def test_the_type_and_area_lists_are_exactly_what_the_parser_keeps():
    raw = {"doc_types": [*CARD_DOC_TYPES, "spaceship"], "domains": [*LIFE_DOMAINS, "astrology"]}
    parsed = parse_query_slots(raw, "anything")
    assert _listed("DOC_TYPES") == list(parsed.doc_types) == slot_doc_types()
    assert _listed("LIFE_DOMAINS") == list(parsed.domains) == slot_domains()
    assert "other" not in _listed("DOC_TYPES") and "other" not in _listed("LIFE_DOMAINS")


def test_the_caps_are_the_parsers():
    values = slots_placeholder_values()
    assert set(values) == {"DOC_TYPES", "LIFE_DOMAINS", "MAX_EXACT_KEYS", "MAX_PEOPLE"}
    assert values["MAX_EXACT_KEYS"] == str(EXACT_KEYS_MAX) == "3"
    assert values["MAX_PEOPLE"] == str(PEOPLE_SLOTS_MAX) == "6"


def test_render_fills_every_placeholder_once():
    out = render_slots_prompt("types: {DOC_TYPES}. areas: {LIFE_DOMAINS}. keys {MAX_EXACT_KEYS}, people {MAX_PEOPLE}.")
    assert out == (f"types: {slots_placeholder_values()['DOC_TYPES']}. areas: {slots_placeholder_values()['LIFE_DOMAINS']}"
                   ". keys 3, people 6.")


def test_a_leftover_placeholder_raises_naming_it():
    with pytest.raises(ValueError, match="UNKNOWN_LIST"):
        render_slots_prompt("types: {DOC_TYPES}; {UNKNOWN_LIST}")


def test_the_schema_requires_every_field_and_its_enums_are_the_code_sets():
    schema = slots_response_schema()
    props = schema["properties"]
    assert schema["required"] == schema["propertyOrdering"] == [
        "subject", "doc_types", "domains", "people", "time", "exact_keys", "scope"]
    assert props["doc_types"]["items"]["enum"] == slot_doc_types()
    assert props["domains"]["items"]["enum"] == slot_domains()
    assert props["scope"]["enum"] == list(SCOPES)
    time = props["time"]
    assert time["required"] == ["intent", "from", "to"]
    assert time["properties"]["intent"]["enum"] == [*TIME_INTENTS, NO_TIME]


def test_the_user_message_carries_the_request_and_today_in_the_users_zone():
    now = datetime(2026, 11, 16, 10, 0, tzinfo=ZoneInfo("UTC")).astimezone(LISBON)
    message = slots_user_message("my Rome hotel booking from August last year", now, "Europe/Lisbon")
    assert message == ('EMAIL_SEARCH_REQUEST "my Rome hotel booking from August last year"\n'
                       "Today: 2026-11-16 (Monday), timezone Europe/Lisbon.")


def test_the_users_day_decides_today_not_utc():
    late = datetime(2026, 11, 16, 23, 30, tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Tokyo"))
    assert "Today: 2026-11-17 (Tuesday), timezone Asia/Tokyo." in slots_user_message("x", late, "Asia/Tokyo")


def test_the_retry_message_names_the_reason():
    assert slots_retry_message("not a JSON object").startswith(
        "Your previous output was not valid query slots (not a JSON object).")


@pytest.mark.parametrize("intent", [NO_TIME, "someday"])
def test_none_and_an_unknown_intent_give_no_time_slot(intent):
    parsed = parse_query_slots({"time": {"intent": intent, "from": "2026-01-01", "to": "2026-01-31"}}, "x")
    assert parsed.time is None
