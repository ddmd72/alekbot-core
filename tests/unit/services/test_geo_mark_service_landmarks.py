"""GeoMarkService with landmarks (GEO_MARKS_RFC §4.4): the confirmation line, the lookup in the mark, the log."""
import logging

import pytest

from src.domain.geo_mark import GeoArea, GeoLandmark, GeoPlace
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk
from tests.unit.services.test_geo_mark_service import ADDRESS, Channel, Geocoder, World, _fix

GYM = GeoLandmark("Iron Temple gym", ("gym",), "BESIDE", 20)
CAFE = GeoLandmark("Café Andorinha", ("cafe",), "ACROSS_THE_ROAD", 150)
LIBRARY = GeoLandmark("Example Library", ("library",), "NEAR", 300)


class PlaceGeocoder(Geocoder):
    def __init__(self, place: GeoPlace) -> None:
        super().__init__()
        self.place = place

    async def look_up(self, latitude, longitude):
        return self.place


def _world(address, landmarks, areas=()) -> World:
    return World(geocoder=PlaceGeocoder(GeoPlace(address, tuple(landmarks), tuple(areas))))


def _line(names, address=ADDRESS) -> str:
    return en.UI_STRINGS[UIMessage.GEO_SAVED_WITH_LANDMARKS.value].format(
        address=address, landmarks=names, accuracy="±5 m", time="18:40")


def _plain(address=ADDRESS) -> str:
    return en.UI_STRINGS[UIMessage.GEO_SAVED.value].format(address=address, accuracy="±5 m", time="18:40")


async def test_the_mark_carries_the_lookups_landmarks_and_areas():
    area = GeoArea("Main Square", "WITHIN")
    mark = await _world(ADDRESS, [GYM], [area]).service.locate(_fix())
    assert mark.address == ADDRESS and mark.landmarks == (GYM,) and mark.areas == (area,)


async def test_the_confirmation_names_the_nearby_landmarks():
    w = _world(ADDRESS, [GYM])
    line = await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en")
    assert line == _line("Iron Temple gym")
    assert line == "📍 Location saved: Main Street, Springfield; nearby: Iron Temple gym (±5 m, 18:40)"


async def test_a_landmark_already_in_the_address_is_skipped():
    w = _world("Iron Temple gym, Main Street, Springfield", [GYM, CAFE])
    line = await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en")
    assert line == _line("Café Andorinha", "Iron Temple gym, Main Street, Springfield")


async def test_all_landmarks_in_the_address_give_the_plain_line():
    w = _world("Iron Temple gym, Main Street, Springfield", [GYM])
    line = await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en")
    assert line == _plain("Iron Temple gym, Main Street, Springfield")


async def test_at_most_the_two_nearest_names_in_lookup_order():
    w = _world(ADDRESS, [GYM, CAFE, LIBRARY])
    line = await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en")
    assert line == _line("Iron Temple gym, Café Andorinha")


async def test_no_landmarks_is_the_plain_line_and_no_address_ignores_landmarks():
    w = _world(ADDRESS, [])
    assert await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en") == _plain()
    w = _world(None, [GYM])
    line = await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                                  channel=Channel(), language="en")
    assert line == en.UI_STRINGS[UIMessage.GEO_SAVED_NO_ADDRESS.value].format(accuracy="±5 m", time="18:40")


async def test_the_history_holds_the_landmark_text_and_the_log_the_counts_only(caplog):
    w = _world(ADDRESS, [GYM, CAFE], [GeoArea("Main Square", "WITHIN")])
    with caplog.at_level(logging.INFO):
        await w.service.record(await w.service.locate(_fix()), user_id="usr-1", session_id="usr-1:C1",
                               channel=Channel(), language="en")
    [(_, [mark_text]), _] = await w.history()
    assert "Iron Temple gym (gym), 20 m away" in mark_text
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "landmarks=2 areas=1" in text and "Iron Temple" not in text and "Main Square" not in text


@pytest.mark.parametrize("locale", [en, uk, fr, es])
def test_the_locale_has_the_landmark_line_with_its_four_fields(locale):
    template = locale.UI_STRINGS[UIMessage.GEO_SAVED_WITH_LANDMARKS.value]
    assert template.format(address="A", landmarks="L", accuracy="±5 m", time="18:40")
    assert all(f"{{{f}}}" in template for f in ("address", "landmarks", "accuracy", "time"))
