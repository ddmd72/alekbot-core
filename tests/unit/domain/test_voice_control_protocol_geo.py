"""The geo frames of protocol v1 (GEO_MARKS_RFC §4.7, VOICE_CALL_CONTROL_CHANNEL_RFC §4.2): `parse_geo_command` turns a
page frame into a validated `GeoFix` and refuses everything else; `geo_state` carries only the fields that apply."""
from datetime import datetime, timezone

import pytest

from src.domain.geo_mark import GeoSource
from src.domain.voice_control_protocol import (
    FEATURE_ASK,
    FEATURE_GEO,
    accepted_commands,
    geo_state_frame,
    parse_geo_command,
)

NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def _frame(**overrides):
    return {"type": "geo", "seq": 1, "latitude": 41.1, "longitude": -8.6, "accuracy": 40, **overrides}


def test_a_good_frame_becomes_a_lelik_button_fix_shared_now():
    fix = parse_geo_command(_frame(), now=NOW)
    assert (fix.latitude, fix.longitude, fix.accuracy_m) == (41.1, -8.6, 40.0)
    assert fix.source is GeoSource.LELIK_BUTTON and fix.shared_at == NOW


def test_without_a_clock_the_fix_is_shared_at_the_relays_now():
    assert parse_geo_command(_frame()).shared_at.tzinfo is not None


def test_a_frame_without_accuracy_is_a_fix_without_accuracy():
    frame = _frame()
    del frame["accuracy"]
    assert parse_geo_command(frame, now=NOW).accuracy_m is None


@pytest.mark.parametrize("bad", [
    {"latitude": None}, {"longitude": None}, {"latitude": "41.1"}, {"longitude": True}, {"latitude": 91},
    {"longitude": -181}, {"latitude": float("nan")}, {"longitude": float("inf")}, {"accuracy": -1},
    {"accuracy": "40"}, {"type": "ask_arm"},
])
def test_a_bad_frame_is_refused(bad):
    with pytest.raises(ValueError):
        parse_geo_command(_frame(**bad), now=NOW)


def test_a_frame_without_coordinates_is_refused():
    with pytest.raises(ValueError):
        parse_geo_command({"type": "geo", "seq": 1}, now=NOW)


def test_geo_state_carries_only_the_fields_given():
    assert geo_state_frame(True, 3, address="12 Oak Street, Springfield", accuracy=40.0) == {
        "type": "geo_state", "ok": True, "seq": 3, "address": "12 Oak Street, Springfield", "accuracy": 40.0}
    assert geo_state_frame(True, 3) == {"type": "geo_state", "ok": True, "seq": 3}
    assert geo_state_frame(False, None, reason="rate") == {"type": "geo_state", "ok": False, "reason": "rate"}


def test_the_geo_feature_accepts_exactly_the_geo_command():
    assert accepted_commands([FEATURE_GEO]) == frozenset({"geo"})
    assert accepted_commands([FEATURE_ASK, FEATURE_GEO]) == frozenset({"ask_arm", "ask_disarm", "geo"})
