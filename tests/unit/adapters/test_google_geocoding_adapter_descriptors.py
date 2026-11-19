"""GoogleGeocodingAdapter address descriptors at the HTTP boundary (GEO_MARKS_RFC §4.3): the request, the landmarks
and areas parsed from `address_descriptor`, and every way that part can be missing or malformed."""
import logging

import pytest

from src.adapters.google_geocoding_adapter import GEOCODE_URL, GoogleGeocodingAdapter
from src.domain.geo_mark import GeoArea, GeoLandmark, GeoPlace
from tests.support import http_boundary_fake

KEY = "geo-key-test"
ADDRESS = "Main Street, Springfield"


@pytest.fixture
def fake(monkeypatch):
    return http_boundary_fake.install(monkeypatch)


def _body(descriptor="absent", **extra):
    body = {"status": "OK", "results": [{"formatted_address": ADDRESS}], **extra}
    if descriptor != "absent":
        body["address_descriptor"] = descriptor
    return body


def _landmark(name, meters=20.4, relation="BESIDE", types=("gym", "establishment", "point_of_interest"), **extra):
    entry = {"display_name": {"text": name, "language_code": "en"}, "place_id": "p", "types": list(types),
             "spatial_relationship": relation, "straight_line_distance_meters": meters, **extra}
    return {k: v for k, v in entry.items() if v is not None}


def _area(name, containment="WITHIN"):
    return {"display_name": {"text": name, "language_code": "en"}, "place_id": "a", "containment": containment}


async def _look_up() -> GeoPlace:
    return await GoogleGeocodingAdapter(KEY).look_up(41.1, -8.6)


async def test_the_request_asks_for_english_and_address_descriptors(fake):
    fake.route("GET", GEOCODE_URL, 200, _body())
    await _look_up()
    [request] = fake.requests
    assert request.params == {"latlng": "41.1,-8.6", "language": "en", "extra_computations": "ADDRESS_DESCRIPTORS",
                              "key": KEY}


async def test_landmarks_and_areas_are_parsed(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({
        "landmarks": [_landmark("Iron Temple gym", 20.4), _landmark("Example Library", 150.6, "ACROSS_THE_ROAD",
                                                                    ("library", "point_of_interest", "book_store"))],
        "areas": [_area("Main Square"), _area("Old Town", "OUTSKIRTS")]}))
    place = await _look_up()
    assert place == GeoPlace(ADDRESS, (
        GeoLandmark("Iron Temple gym", ("gym",), "BESIDE", 20),
        GeoLandmark("Example Library", ("library", "book store"), "ACROSS_THE_ROAD", 151)),
        (GeoArea("Main Square", "WITHIN"), GeoArea("Old Town", "OUTSKIRTS")))


async def test_landmarks_are_sorted_nearest_first_whatever_the_response_order(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({"landmarks": [
        _landmark("Example Library", 207), _landmark("Iron Temple gym", 29), _landmark("Café Andorinha", 308)]}))
    place = await _look_up()
    assert [x.name for x in place.landmarks] == ["Iron Temple gym", "Example Library", "Café Andorinha"]
    assert [x.distance_m for x in place.landmarks] == [29, 207, 308]


async def test_an_unknown_relation_becomes_none(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({"landmarks": [
        _landmark("Iron Temple gym", relation="SIDEWAYS"), _landmark("Example Library", relation=None)]}))
    place = await _look_up()
    assert [x.relation for x in place.landmarks] == [None, None]


async def test_a_landmark_without_a_name_or_a_numeric_distance_is_skipped(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({"landmarks": [
        {"display_name": {"text": "  "}, "straight_line_distance_meters": 5},
        {"straight_line_distance_meters": 5},
        _landmark("No distance", meters=None),
        _landmark("Bad distance", meters="far"),
        _landmark("Bool distance", meters=True),
        "junk",
        _landmark("Iron Temple gym", 20)]}))
    assert [x.name for x in (await _look_up()).landmarks] == ["Iron Temple gym"]


async def test_an_area_without_a_name_or_with_another_containment_is_skipped(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({"areas": [
        _area(""), _area("Nowhere", "ELSEWHERE"), {"display_name": {"text": "Old Town"}}, _area("Main Square", "NEAR")]}))
    assert (await _look_up()).areas == (GeoArea("Main Square", "NEAR"),)


async def test_at_most_five_landmarks_and_three_areas(fake):
    fake.route("GET", GEOCODE_URL, 200, _body({
        "landmarks": [_landmark(f"Place {i}", 10 * i) for i in range(1, 9)],
        "areas": [_area(f"Area {i}") for i in range(5)]}))
    place = await _look_up()
    assert len(place.landmarks) == 5 and len(place.areas) == 3


@pytest.mark.parametrize("descriptor", ["absent", {}, {"landmarks": [], "areas": []}, None])
async def test_a_missing_or_empty_descriptor_keeps_the_address(fake, descriptor):
    fake.route("GET", GEOCODE_URL, 200, _body(descriptor))
    assert await _look_up() == GeoPlace(ADDRESS, (), ())


@pytest.mark.parametrize("descriptor", ["nope", [1], {"landmarks": "x"}, {"landmarks": [], "areas": 3}])
async def test_a_malformed_descriptor_keeps_the_address_without_landmarks(fake, caplog, descriptor):
    fake.route("GET", GEOCODE_URL, 200, _body(descriptor))
    with caplog.at_level(logging.DEBUG):
        assert await _look_up() == GeoPlace(ADDRESS, (), ())


@pytest.mark.parametrize("status,body", [(200, {"status": "ZERO_RESULTS", "results": []}), (500, "boom")])
async def test_a_failure_is_the_empty_place(fake, status, body):
    fake.route("GET", GEOCODE_URL, status, body)
    assert await _look_up() is GeoPlace.EMPTY


async def test_logs_carry_no_names_coordinates_address_or_key(fake, caplog):
    fake.route("GET", GEOCODE_URL, 200, _body({"landmarks": [_landmark("Iron Temple gym")], "areas": ["bad"]}))
    with caplog.at_level(logging.DEBUG):
        await _look_up()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert not any(s in text for s in ("41.1", "-8.6", ADDRESS, KEY, "Iron Temple"))
