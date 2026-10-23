"""parse_enum and parse_reported_date, shared by the update and create parsers."""

from datetime import datetime, timezone

import pytest

from src.domain.entities import FactState
from src.domain.fact_argument_parsing import parse_enum, parse_reported_date


def test_enum_is_case_and_space_insensitive():
    assert parse_enum(FactState, "state", " ARCHIVED ") is FactState.ARCHIVED


def test_enum_error_lists_every_allowed_value():
    with pytest.raises(ValueError) as caught:
        parse_enum(FactState, "state", "zombie")
    assert str(caught.value) == (
        "Invalid state 'zombie'; use one of: current, stale, archived, superseded, invalidated")


def test_enum_rejects_a_non_string():
    with pytest.raises(ValueError, match="state must be a string"):
        parse_enum(FactState, "state", None)


@pytest.mark.parametrize("raw", [None, ""])
def test_no_date(raw):
    assert parse_reported_date(raw) is None


def test_date_forms():
    assert parse_reported_date("2020-01-02T03:04:05Z") == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    given = datetime(2021, 1, 1, tzinfo=timezone.utc)
    assert parse_reported_date(given) is given


@pytest.mark.parametrize("raw", ["soon", 5])
def test_bad_date(raw):
    with pytest.raises(ValueError, match="reported_date"):
        parse_reported_date(raw)


@pytest.mark.parametrize("raw,expected", [
    ("2020-01-02", datetime(2020, 1, 2, tzinfo=timezone.utc)),
    ("2020-01-02T03:04:05", datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)),
    (datetime(2020, 1, 2, 3, 4, 5), datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc))])
def test_a_naive_date_is_utc(raw, expected):
    parsed = parse_reported_date(raw)
    assert parsed == expected and parsed.tzinfo is not None


def test_an_offset_date_keeps_its_offset():
    parsed = parse_reported_date("2020-01-02T03:04:05+02:00")
    assert parsed.utcoffset().total_seconds() == 7200
