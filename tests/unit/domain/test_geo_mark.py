"""Geo marks, the domain (GEO_MARKS_RFC §4.1, §4.2): the fix's validation, the wire and record forms, and the texts the
model reads. Only the structural facts are pinned, not the wording."""
import re
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.geo_mark import (
    LINK_ID_PATTERN, PRECISE_ACCURACY_M, GeoFix, GeoLinkBinding, GeoMark, GeoSource, accuracy_band, local_shared_at,
    render_call_note, render_mark_text,
)

AT = datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc)
ADDRESS = "Main Street, Springfield"
BINDING = GeoLinkBinding(user_id="usr-1", account_id="acc-1", platform="slack", channel_id="C1", session_id="usr-1:C1",
                         platform_user_id="U1", language="en", text="Is the Iron Temple gym open?")


def _fix(accuracy=5.0, source=GeoSource.SLACK_LINK, lat=41.1, lon=-8.6) -> GeoFix:
    return GeoFix.parse(lat, lon, accuracy, AT, source)


class TestGeoFixParse:

    @pytest.mark.parametrize("lat,lon", [(90, 180), (-90, -180), (0, 0), (41.1, -8.6)])
    def test_the_edges_are_accepted(self, lat, lon):
        fix = GeoFix.parse(lat, lon, 0, AT, GeoSource.TELEGRAM_PIN)
        assert (fix.latitude, fix.longitude, fix.accuracy_m) == (float(lat), float(lon), 0.0)
        assert isinstance(fix.latitude, float) and fix.source is GeoSource.TELEGRAM_PIN

    def test_no_accuracy_is_kept_as_none(self):
        assert GeoFix.parse(41.1, -8.6, None, AT, GeoSource.TELEGRAM_PIN).accuracy_m is None

    def test_shared_at_is_normalised_to_utc(self):
        local = AT.astimezone(timezone(timedelta(hours=2)))
        fix = GeoFix.parse(41.1, -8.6, 5, local, GeoSource.SLACK_LINK)
        assert fix.shared_at == AT and fix.shared_at.utcoffset() == timedelta(0)

    def test_the_source_value_is_read_as_the_enum(self):
        assert GeoFix.parse(41.1, -8.6, 5, AT, "slack_geo").source is GeoSource.SLACK_LINK  # type: ignore[arg-type]

    @pytest.mark.parametrize("lat,lon,acc", [
        (float("nan"), 0, 5), (0, float("inf"), 5), (0, 0, float("-inf")), (0, 0, float("nan")),
        (90.0001, 0, 5), (-90.0001, 0, 5), (0, 180.0001, 5), (0, -180.0001, 5),
        (0, 0, -1), (0, 0, -0.5),
        (True, 0, 5), (0, False, 5), (0, 0, True),
        ("41.1", 0, 5), (0, "-8.6", 5), (0, 0, "5"), (None, 0, 5), (0, [1], 5),
    ])
    def test_a_bad_value_is_refused(self, lat, lon, acc):
        with pytest.raises(ValueError):
            GeoFix.parse(lat, lon, acc, AT, GeoSource.SLACK_LINK)

    def test_a_naive_time_is_refused(self):
        with pytest.raises(ValueError):
            GeoFix.parse(41.1, -8.6, 5, AT.replace(tzinfo=None), GeoSource.SLACK_LINK)

    def test_an_unknown_source_is_refused(self):
        with pytest.raises(ValueError):
            GeoFix.parse(41.1, -8.6, 5, AT, "maps_link")  # type: ignore[arg-type]


class TestWireAndRecord:

    @pytest.mark.parametrize("mark", [
        GeoMark(_fix(), ADDRESS),
        GeoMark(_fix(accuracy=None, source=GeoSource.TELEGRAM_PIN), None),
        GeoMark(_fix(accuracy=250, source=GeoSource.LELIK_BUTTON), "12 Oak Street, Springfield"),
    ])
    def test_a_mark_round_trips(self, mark):
        assert GeoMark.from_wire(mark.to_wire()) == mark

    def test_the_wire_form_is_plain_json_values(self):
        wire = GeoMark(_fix(), ADDRESS).to_wire()
        assert wire == {"latitude": 41.1, "longitude": -8.6, "accuracy_m": 5.0, "shared_at": AT.isoformat(),
                        "source": "slack_geo", "address": ADDRESS}

    def test_a_bad_wire_value_is_refused(self):
        wire = GeoMark(_fix(), ADDRESS).to_wire()
        with pytest.raises(ValueError):
            GeoMark.from_wire({**wire, "latitude": 91})
        with pytest.raises(ValueError):
            GeoMark.from_wire({**wire, "address": 7})
        with pytest.raises(KeyError):
            GeoMark.from_wire({k: v for k, v in wire.items() if k != "longitude"})

    def test_a_binding_round_trips_and_its_record_holds_exactly_its_fields(self):
        record = BINDING.to_record()
        assert GeoLinkBinding.from_record(record) == BINDING
        assert set(record) == {"user_id", "account_id", "platform", "channel_id", "session_id", "platform_user_id",
                               "language", "text"}
        assert "expires_at" not in record      # the store writes the expiry; the binding never does

    def test_an_empty_text_is_a_binding_without_a_question(self):
        record = {**BINDING.to_record(), "text": ""}
        assert GeoLinkBinding.from_record(record).text == ""

    @pytest.mark.parametrize("field", ["user_id", "session_id", "text"])
    def test_a_record_with_a_missing_or_non_string_field_is_refused(self, field):
        with pytest.raises(ValueError):
            GeoLinkBinding.from_record({k: v for k, v in BINDING.to_record().items() if k != field})
        with pytest.raises(ValueError):
            GeoLinkBinding.from_record({**BINDING.to_record(), field: None})


class TestBandsAndConstants:

    def test_the_accuracy_band(self):
        assert accuracy_band(GeoMark(_fix(accuracy=PRECISE_ACCURACY_M), None)) == "precise"
        assert accuracy_band(GeoMark(_fix(accuracy=0), None)) == "precise"
        assert accuracy_band(GeoMark(_fix(accuracy=PRECISE_ACCURACY_M + 0.5), None)) == "approx"
        assert accuracy_band(GeoMark(_fix(accuracy=None), None)) == "none"

    def test_the_link_id_is_22_url_safe_characters(self):
        assert LINK_ID_PATTERN.fullmatch("A" * 21 + "-")
        assert LINK_ID_PATTERN.fullmatch("a_" * 11)
        assert not LINK_ID_PATTERN.fullmatch("A" * 21)
        assert not LINK_ID_PATTERN.fullmatch("A" * 23)
        assert not LINK_ID_PATTERN.fullmatch("A" * 21 + "/")


# RFC §4.2's table: (accuracy, address, source) per row.
ROWS = [
    pytest.param(5.0, ADDRESS, GeoSource.SLACK_LINK, id="precise-address-slack"),
    pytest.param(40.0, None, GeoSource.SLACK_LINK, id="precise-no-address-slack"),
    pytest.param(250.0, ADDRESS, GeoSource.SLACK_LINK, id="approx-address-slack"),
    pytest.param(5.0, ADDRESS, GeoSource.TELEGRAM_PIN, id="precise-address-telegram"),
    pytest.param(250.0, None, GeoSource.TELEGRAM_PIN, id="approx-no-address-telegram"),
    pytest.param(None, ADDRESS, GeoSource.TELEGRAM_PIN, id="no-accuracy-address-telegram"),
    pytest.param(None, None, GeoSource.TELEGRAM_PIN, id="no-accuracy-no-address-telegram"),
]


def _no_system_voice(text: str) -> None:
    assert "[System" not in text and "System:" not in text and "SYSTEM" not in text


class TestRenderMarkText:

    @pytest.mark.parametrize("accuracy,address,source", ROWS)
    def test_every_row_has_the_record_shape(self, accuracy, address, source):
        mark = GeoMark(_fix(accuracy=accuracy, source=source), address)

        text = render_mark_text(mark, "UTC")

        assert text.startswith("[Geo mark:")
        _no_system_voice(text)
        assert "41.10000, -8.60000" in text
        assert "Shared at: 2026-11-18 18:40 (UTC+00:00)" in text
        assert (ADDRESS in text) == (address is not None)
        assert "Is the Iron Temple gym open?" not in text

    @pytest.mark.parametrize("accuracy,address,source", ROWS)
    def test_the_rows_are_told_apart(self, accuracy, address, source):
        text = render_mark_text(GeoMark(_fix(accuracy=accuracy, source=source), address), "UTC")
        if accuracy is None:
            assert "no accuracy" in text and "picked on the map" in text and "their position at that moment" not in text
        else:
            assert f"about {round(accuracy)} m" in text and "their position at that moment" in text
            assert ("approximate" in text) == (accuracy > PRECISE_ACCURACY_M)
        if address is None:
            assert "not available" in text
        else:
            assert "looked up from the position, not stated by the user" in text
        assert ("Slack" in text) == (source is GeoSource.SLACK_LINK)
        assert ("Telegram" in text) == (source is GeoSource.TELEGRAM_PIN)

    def test_an_approximate_fix_gives_kilometres_too(self):
        text = render_mark_text(GeoMark(_fix(accuracy=250), None), "UTC")
        assert "0.3 km" in text

    def test_a_fix_without_accuracy_from_another_source_is_not_a_point(self):
        mark = GeoMark(_fix(accuracy=None, source=GeoSource.LELIK_BUTTON), None)
        assert render_mark_text(mark, "UTC").startswith("[Geo mark: the user shared their position at that moment,")
        assert render_call_note(mark).startswith("[The caller has just shared their position at that moment,")

    @pytest.mark.parametrize("zone,expected", [
        ("Europe/Lisbon", "Shared at: 2026-11-18 18:40 (UTC+00:00)"),
        ("Europe/Berlin", "Shared at: 2026-11-18 19:40 (UTC+01:00)"),
        ("America/New_York", "Shared at: 2026-11-18 13:40 (UTC-05:00)"),
        ("Asia/Kolkata", "Shared at: 2026-11-19 00:10 (UTC+05:30)"),
    ])
    def test_the_time_is_the_owners_clock_with_its_offset(self, zone, expected):
        assert expected in render_mark_text(GeoMark(_fix(), ADDRESS), zone)

    @pytest.mark.parametrize("zone", ["", "Mars/Olympus", "../etc", "not a zone"])
    def test_an_unknown_zone_is_utc(self, zone):
        assert "Shared at: 2026-11-18 18:40 (UTC+00:00)" in render_mark_text(GeoMark(_fix(), ADDRESS), zone)

    def test_coordinates_carry_five_decimals(self):
        mark = GeoMark(GeoFix.parse(41.123456789, -8.6, 5, AT, GeoSource.SLACK_LINK), None)
        assert re.search(r"Position: 41\.12346, -8\.60000", render_mark_text(mark, "UTC"))

    def test_local_shared_at_is_the_aware_local_instant(self):
        local = local_shared_at(GeoMark(_fix(), None), "Europe/Berlin")
        assert local.hour == 19 and local == AT


class TestRenderCallNote:

    @pytest.mark.parametrize("accuracy,address,source", ROWS)
    def test_every_row_has_the_note_shape(self, accuracy, address, source):
        note = render_call_note(GeoMark(_fix(accuracy=accuracy, source=source), address))
        assert note.startswith("[") and note.endswith("]")
        _no_system_voice(note)
        assert "41.10000, -8.60000" in note
        assert (ADDRESS in note) == (address is not None)
        assert "Is the Iron Temple gym open?" not in note
