"""Transcript stamps are MM:SS / H:MM:SS strings, never integer seconds (RFC §27.5, Δ128)."""
import itertools

import pytest

from src.domain.drive_transcript_time import format_stamp, parse_stamp


def test_parse_mm_ss():
    assert parse_stamp("12:40") == 760.0
    assert parse_stamp("2:05") == 125.0
    assert parse_stamp("00:00") == 0.0
    assert parse_stamp("59:59") == 3599.0


def test_parse_h_mm_ss():
    assert parse_stamp("1:00:00") == 3600.0
    assert parse_stamp("1:02:03") == 3723.0
    assert parse_stamp("12:59:59") == 12 * 3600 + 59 * 60 + 59


@pytest.mark.parametrize("bad", ["75", "1:75", "-1:00", "", "abc", "1:2", "1:60", "1:60:00", "1:00:60", "1:2:3:4",
                                 "12:40.5", " ", "1.5", "75:00", "0:-5"])
def test_rejects_integer_seconds_and_garbage(bad):
    with pytest.raises(ValueError):
        parse_stamp(bad)


def test_format_switches_at_one_hour():
    assert format_stamp(0) == "00:00"
    assert format_stamp(59) == "00:59"
    assert format_stamp(3599) == "59:59"
    assert format_stamp(3600) == "1:00:00"
    assert format_stamp(3723) == "1:02:03"
    assert format_stamp(760.9) == "12:40"


def test_format_rejects_negative():
    with pytest.raises(ValueError):
        format_stamp(-1)


def test_roundtrip_property():
    for seconds in itertools.chain(range(0, 4000), range(3600 * 3, 3600 * 4 + 200, 7)):
        assert parse_stamp(format_stamp(seconds)) == float(seconds)
    for stamp in ("00:00", "12:40", "59:59", "1:00:00", "3:59:59"):
        assert format_stamp(parse_stamp(stamp)) == stamp
