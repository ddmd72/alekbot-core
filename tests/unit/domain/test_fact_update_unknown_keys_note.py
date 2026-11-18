"""P14-D1: `parse_fact_update` names the keys it ignored, so an update of only unknown keys is not silently `updated`."""
from src.domain.fact_update import UNKNOWN_KEYS_IGNORED, UPDATE_KEYS, parse_fact_update


def test_unknown_keys_are_named_in_one_sorted_note_and_known_keys_still_parse():
    parsed = parse_fact_update({"content": "User works at Acme.", "domain": "work", "context_priority": "low"})
    assert parsed.content == "User works at Acme."
    assert parsed.notes == (UNKNOWN_KEYS_IGNORED.format(keys="context_priority, domain"),)


def test_only_known_keys_add_no_note():
    assert parse_fact_update({"content": "User works at Acme.", "as_history": False}).notes == ()


def test_the_unknown_keys_note_follows_the_expiry_note():
    parsed = parse_fact_update({"state": "ARCHIVED", "expiry": {"kind": "none"}, "context": "Q1"})
    assert len(parsed.notes) == 2 and parsed.notes[1] == UNKNOWN_KEYS_IGNORED.format(keys="context")


def test_every_parsed_field_is_a_known_key():
    assert UPDATE_KEYS == {"content", "tags", "metadata", "state", "temporal_class", "reported_date", "superseded_by",
                           "expiry", "as_history"}
