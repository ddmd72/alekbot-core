"""parse_new_fact and the S3 fields: `expiry`, `derived_from`, no `ttl_days`, no STALE (Facts S3, plan §3.4, §3.5)."""

import dataclasses

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE, Expiry
from src.domain.new_fact_attributes import NewFactAttributes, parse_new_fact
from tests.support.fact_write import EXPIRY_NONE, horizon, on


def test_the_model_ttl_days_is_not_read_even_when_malformed():
    for ttl in (30, "soon", [1], -4, None):
        parsed = parse_new_fact({"text": "a fact", "ttl_days": ttl})
        assert not hasattr(parsed, "ttl_days") and parsed.expiry is None
    assert "ttl_days" not in {f.name for f in dataclasses.fields(NewFactAttributes)}


def test_stale_is_refused_on_create():
    with pytest.raises(ValueError, match="cannot have state STALE: only the system marks a fact stale"):
        parse_new_fact({"text": "a fact", "state": "stale"})


def test_superseded_is_still_refused_on_create():
    with pytest.raises(ValueError, match="cannot have state SUPERSEDED"):
        parse_new_fact({"text": "a fact", "state": "superseded"})


@pytest.mark.parametrize("state", ["current", "archived", "invalidated"])
def test_the_other_states_are_kept(state):
    assert parse_new_fact({"text": "a fact", "state": state}).state is FactState(state)


def test_an_expiry_is_parsed_and_optional():
    assert parse_new_fact({"text": "a fact"}).expiry is None
    assert parse_new_fact({"text": "a fact", "expiry": horizon(14)}).expiry == Expiry(ExpiryKind.HORIZON, days=14)
    assert parse_new_fact({"text": "a fact", "expiry": on("2026-11-02")}).expiry.kind is ExpiryKind.DATE
    assert parse_new_fact({"text": "a fact", "expiry": EXPIRY_NONE}).expiry.kind is ExpiryKind.NONE


def test_a_null_or_malformed_expiry_on_a_current_fact_is_refused():
    with pytest.raises(FactExpiryError, match="`null` is not an answer"):
        parse_new_fact({"text": "a fact", "expiry": None})
    with pytest.raises(FactExpiryError, match="Invalid expiry"):
        parse_new_fact({"text": "a fact", "expiry": {"kind": "date"}})


@pytest.mark.parametrize("state", ["archived", "invalidated"])
def test_an_expiry_sent_with_a_closed_state_is_optional_and_dropped_with_a_note(state):
    parsed = parse_new_fact({"text": "the meeting was on Monday", "state": state, "expiry": on("2026-10-01")})
    assert parsed.expiry is None and parsed.notes == (EXPIRY_IGNORED_ON_CLOSE,)
    assert parse_new_fact({"text": "the meeting was on Monday", "state": state}).notes == ()


def test_a_malformed_expiry_with_a_closed_state_does_not_fail_the_create():
    assert parse_new_fact({"text": "x", "state": "archived", "expiry": "soon"}).expiry is None


def test_derived_from_is_a_tuple_of_ids_from_a_string_or_a_list():
    assert parse_new_fact({"text": "x"}).derived_from == ()
    assert parse_new_fact({"text": "x", "derived_from": None}).derived_from == ()
    assert parse_new_fact({"text": "x", "derived_from": "f1"}).derived_from == ("f1",)
    assert parse_new_fact({"text": "x", "derived_from": ["f1", " f2 ", "f1"]}).derived_from == ("f1", "f2")
    assert parse_new_fact({"text": "x", "derived_from": []}).derived_from == ()


@pytest.mark.parametrize("bad", [3, {"id": "f1"}, ["f1", 2], [""], "", "  ", [None]])
def test_a_derived_from_that_is_not_ids_is_refused(bad):
    with pytest.raises(ValueError, match="derived_from must be a fact id or a list of fact ids"):
        parse_new_fact({"text": "x", "derived_from": bad})


def test_expiry_fields_start_empty_and_are_set_with_replace():
    parsed = parse_new_fact({"text": "x", "expiry": EXPIRY_NONE})
    assert parsed.expiry_fields is None
    decided = dataclasses.replace(parsed, expiry_fields={"expiry_kind": ExpiryKind.NONE})
    assert decided.expiry_fields == {"expiry_kind": ExpiryKind.NONE} and parsed.expiry_fields is None
