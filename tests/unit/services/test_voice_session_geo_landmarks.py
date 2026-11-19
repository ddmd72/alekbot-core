"""The call note of a web call carries the landmarks and areas of the lookup (GEO_MARKS_RFC §4.7)."""
from datetime import datetime, timezone

import pytest

from src.domain.geo_mark import GeoArea, GeoFix, GeoLandmark, GeoMark, GeoPlace, GeoSource, render_call_note
from src.ports.reverse_geocoding_port import ReverseGeocodingPort
from tests.unit.services.test_voice_session_geo import ADDRESS, _Call, _geo

GYM = GeoLandmark("Iron Temple gym", ("gym",), "BESIDE", 20)
SQUARE = GeoArea("Main Square", "WITHIN")


class _PlaceGeocoder(ReverseGeocodingPort):
    def __init__(self, place: GeoPlace) -> None:
        self.place = place

    async def look_up(self, latitude: float, longitude: float) -> GeoPlace:
        return self.place


@pytest.mark.asyncio
async def test_the_call_note_carries_landmarks_and_areas_and_the_state_frame_only_the_address():
    call = await _Call(None, _PlaceGeocoder(GeoPlace(ADDRESS, (GYM,), (SQUARE,)))).start()
    await call.page_says(_geo())
    fix = GeoFix.parse(41.1, -8.6, 40, datetime.now(timezone.utc), GeoSource.LELIK_BUTTON)
    assert call.geo_notes() == [render_call_note(GeoMark(fix, ADDRESS, (GYM,), (SQUARE,)))]
    assert "Iron Temple gym (gym), 20 m away, the position is beside it" in call.geo_notes()[0]
    assert "areas: the position is within Main Square" in call.geo_notes()[0]
    assert call.geo_states() == [{"type": "geo_state", "ok": True, "seq": 1, "address": ADDRESS, "accuracy": 40.0}]
    await call.end()
