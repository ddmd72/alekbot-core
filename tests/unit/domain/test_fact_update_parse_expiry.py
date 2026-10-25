"""parse_fact_update and `expiry`: STALE refused, a closing state drops the decision with a note (Facts S3, plan §3.4)."""

import pytest

from src.domain.entities import ExpiryKind, FactState
from src.domain.exceptions import FactExpiryError
from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE, EXPIRY_NONE, Expiry
from src.domain.fact_update import ParsedFactUpdate, parse_fact_update
from tests.support.fact_write import horizon, on


def test_stale_is_refused_with_the_way_to_retire_a_fact():
    with pytest.raises(ValueError) as refused:
        parse_fact_update({"state": "STALE"})
    assert str(refused.value) == (
        "only the system marks a fact stale; to retire a fact use ARCHIVED, INVALIDATED or SUPERSEDED")


@pytest.mark.parametrize("spelling", ["stale", "Stale", " STALE "])
def test_stale_is_refused_in_any_spelling(spelling):
    with pytest.raises(ValueError, match="only the system marks a fact stale"):
        parse_fact_update({"state": spelling})


def test_stale_is_refused_before_the_superseded_by_rule_speaks():
    with pytest.raises(ValueError, match="only the system marks a fact stale"):
        parse_fact_update({"state": "STALE", "superseded_by": "f2"})


def test_an_expiry_is_parsed_into_the_update():
    assert parse_fact_update({"expiry": horizon(14)}).expiry == Expiry(ExpiryKind.HORIZON, days=14)
    assert parse_fact_update({"expiry": "none"}).expiry is EXPIRY_NONE
    assert parse_fact_update({"expiry": on("2026-10-30"), "state": "CURRENT"}).expiry.kind is ExpiryKind.DATE


def test_no_expiry_key_means_no_decision_and_no_note():
    assert parse_fact_update({"content": "x"}) == ParsedFactUpdate(content="x")


def test_a_null_expiry_is_refused_with_the_null_text():
    with pytest.raises(FactExpiryError, match="`null` is not an answer"):
        parse_fact_update({"content": "x", "expiry": None})


def test_a_malformed_expiry_is_a_fact_expiry_error_and_a_value_error():
    with pytest.raises(FactExpiryError, match="Invalid expiry") as refused:
        parse_fact_update({"expiry": {"kind": "horizon", "days": 0}})
    assert isinstance(refused.value, ValueError)


@pytest.mark.parametrize(
    "extra", [{"state": "ARCHIVED"}, {"state": "INVALIDATED"}, {"state": "SUPERSEDED", "superseded_by": "f2"}]
)
def test_an_expiry_sent_with_a_closing_state_is_dropped_with_a_note(extra):
    parsed = parse_fact_update({"expiry": horizon(10), **extra})
    assert parsed.expiry is None and parsed.notes == (EXPIRY_IGNORED_ON_CLOSE,)
    assert parsed.state is FactState(extra["state"].lower())


def test_a_malformed_expiry_with_a_closing_state_is_ignored_too():
    assert parse_fact_update({"expiry": {"kind": "soon"}, "state": "ARCHIVED"}).expiry is None


def test_a_null_expiry_with_a_closing_state_has_nothing_to_note():
    assert parse_fact_update({"expiry": None, "state": "ARCHIVED"}).notes == ()


def test_the_ignore_note_text():
    assert EXPIRY_IGNORED_ON_CLOSE == "expiry is not used when a fact is closed; ignored"
