"""Geo mark texts after the model-facing wording review (GEO_MARKS_RFC §4.2, §4.7): the map-point reading only for a
Telegram pin without accuracy, the call note split by source and consistent under every accuracy value, and no line
that reads as a standing rule once consolidated."""
from datetime import datetime, timezone

import pytest

from src.domain.geo_mark import GeoFix, GeoMark, GeoSource, render_call_note, render_mark_text

AT = datetime(2026, 11, 18, 18, 40, tzinfo=timezone.utc)
ADDRESS = "Main Street, Springfield"
UNKNOWN_CLOSENESS = " — no accuracy came with it, so how close it is to the spot is unknown"


def _mark(accuracy, source, address=ADDRESS) -> GeoMark:
    return GeoMark(GeoFix.parse(41.1, -8.6, accuracy, AT, source), address)


class TestMarkText:

    @pytest.mark.parametrize("source", [GeoSource.SLACK_LINK, GeoSource.LELIK_BUTTON])
    def test_a_browser_fix_without_accuracy_is_their_position_of_unknown_closeness(self, source):
        text = render_mark_text(_mark(None, source), "UTC")
        assert text.startswith("[Geo mark: the user shared their position at that moment, on purpose,")
        assert f"41.10000, -8.60000{UNKNOWN_CLOSENESS}\n" in text
        assert "picked on the map" not in text and "a point" not in text

    def test_a_telegram_pin_without_accuracy_is_a_point_that_may_be_picked_on_the_map(self):
        text = render_mark_text(_mark(None, GeoSource.TELEGRAM_PIN), "UTC")
        header = text.split("\n", 1)[0]
        assert header == "[Geo mark: the user shared a point, on purpose, as a Telegram location]"
        assert header.count("Telegram") == 1
        assert "may be a point picked on the map rather than where the user was" in text
        assert "how close it is to the spot is unknown" not in text

    @pytest.mark.parametrize("source", list(GeoSource))
    @pytest.mark.parametrize("accuracy", [5.0, 40.0, 250.0])
    def test_a_fix_with_accuracy_is_their_position_at_that_moment(self, source, accuracy):
        text = render_mark_text(_mark(accuracy, source), "UTC")
        assert text.split("\n", 1)[0].startswith("[Geo mark: the user shared their position at that moment, on purpose,")
        assert "current position" not in text and "no accuracy" not in text

    def test_an_approximate_fix_names_the_area_without_a_network_claim(self):
        text = render_mark_text(_mark(250.0, GeoSource.SLACK_LINK), "UTC")
        assert ", approximate: within about 250 m (or 0.3 km), so the address names the area, not the exact spot\n" in text
        assert "network" not in text

    def test_no_address_says_the_lookup_returned_none(self):
        text = render_mark_text(_mark(5.0, GeoSource.SLACK_LINK, address=None), "UTC")
        assert "Nearest street address: not available (the lookup returned no address)\n" in text
        assert "did not answer" not in text

    @pytest.mark.parametrize("source", list(GeoSource))
    @pytest.mark.parametrize("accuracy", [None, 5.0, 250.0])
    def test_the_closing_describes_and_never_rules(self, source, accuracy):
        closing = render_mark_text(_mark(accuracy, source), "UTC").split("\n")[-1]
        assert closing.startswith("This records the position the user shared at that moment,")
        assert "later, it shows only where they were at its 'Shared at' time" in closing
        assert "the home location stays the reference" in closing
        assert "names its age" not in closing and "where the user was at that moment" not in closing


class TestCallNote:

    @pytest.mark.parametrize("source,phrase", [
        (GeoSource.LELIK_BUTTON, "from the voice call page"),
        (GeoSource.SLACK_LINK, "from the location link in Slack"),
        (GeoSource.TELEGRAM_PIN, "as a Telegram location"),
    ])
    def test_the_note_names_its_source(self, source, phrase):
        note = render_call_note(_mark(5.0, source))
        assert note.startswith(f"[The caller has just shared their position at that moment, on purpose, {phrase}: "
                               "41.10000, -8.60000, accurate to about 5 m;")

    @pytest.mark.parametrize("source", [GeoSource.LELIK_BUTTON, GeoSource.SLACK_LINK])
    def test_a_browser_fix_without_accuracy_has_unknown_closeness(self, source):
        note = render_call_note(_mark(None, source))
        assert f"41.10000, -8.60000{UNKNOWN_CLOSENESS};" in note and "picked on the map" not in note

    def test_a_telegram_point_without_accuracy_speaks_of_the_caller(self):
        note = render_call_note(_mark(None, GeoSource.TELEGRAM_PIN))
        assert note.startswith("[The caller has just shared a point, on purpose, as a Telegram location: ")
        assert "rather than where the caller was" in note and "the user" not in note

    @pytest.mark.parametrize("source", list(GeoSource))
    @pytest.mark.parametrize("accuracy,address", [(None, None), (5.0, ADDRESS), (250.0, None)])
    def test_the_note_claims_nothing_its_accuracy_cannot_hold(self, source, accuracy, address):
        note = render_call_note(_mark(accuracy, source, address))
        assert "where they are now" not in note and "current position" not in note
        assert note.endswith("They shared it for what they ask with it. Do not read the coordinates aloud.]")
        if address is None:
            assert "nearest street address: not available (the lookup returned no address)." in note
