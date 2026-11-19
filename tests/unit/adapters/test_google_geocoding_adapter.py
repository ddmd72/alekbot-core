"""GoogleGeocodingAdapter at the HTTP boundary (GEO_MARKS_RFC §4.3): the request it sends, the first formatted address,
and every failure read as no address without a raise; no log line carries the position or the address."""
import asyncio
import logging

import pytest

from src.adapters.google_geocoding_adapter import GEOCODE_URL, GoogleGeocodingAdapter
from tests.support import http_boundary_fake

KEY = "geo-key-test"
ADDRESS = "Main Street, Springfield"
OK = {"status": "OK", "results": [{"formatted_address": ADDRESS}, {"formatted_address": "Shelbyville"}]}


@pytest.fixture
def fake(monkeypatch):
    return http_boundary_fake.install(monkeypatch)


async def _lookup(language="en"):
    return await GoogleGeocodingAdapter(KEY).nearest_address(41.1, -8.6, language=language)


def _no_position_or_address_logged(caplog):
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "41.1" not in text and "-8.6" not in text and ADDRESS not in text and KEY not in text


async def test_the_request_carries_the_position_the_language_and_the_key(fake):
    fake.route("GET", GEOCODE_URL, 200, OK)

    assert await _lookup(language="uk") == ADDRESS

    [request] = fake.requests
    assert request.method == "GET" and request.url == GEOCODE_URL
    assert request.params == {"latlng": "41.1,-8.6", "language": "uk", "key": KEY}


async def test_ok_gives_the_first_formatted_address_stripped(fake):
    fake.route("GET", GEOCODE_URL, 200, {"status": "OK", "results": [{"formatted_address": f"  {ADDRESS} "}]})
    assert await _lookup() == ADDRESS


@pytest.mark.parametrize("status,body", [
    (200, {"status": "ZERO_RESULTS", "results": []}),
    (200, {"status": "REQUEST_DENIED", "error_message": "API not enabled"}),
    (200, {"status": "OVER_QUERY_LIMIT"}),
    (200, {"status": "OK", "results": []}),
    (200, {"status": "OK", "results": [{"place_id": "x"}]}),
    (200, {"status": "OK", "results": [{"formatted_address": "  "}]}),
    (200, {"status": "OK", "results": "nope"}),
    (200, {"results": [{"formatted_address": ADDRESS}]}),
    (200, ["not", "an", "object"]),
    (200, "not json {"),
    (500, {"status": "UNKNOWN_ERROR"}),
    (403, "forbidden"),
])
async def test_every_other_answer_is_no_address_without_a_raise(fake, caplog, status, body):
    fake.route("GET", GEOCODE_URL, status, body)
    with caplog.at_level(logging.DEBUG):
        assert await _lookup() is None
    assert any("[Geocoding]" in r.getMessage() for r in caplog.records)
    _no_position_or_address_logged(caplog)


@pytest.mark.parametrize("error", [asyncio.TimeoutError(), ConnectionResetError("reset")])
async def test_a_timeout_or_a_transport_error_is_no_address(fake, caplog, error):
    def boom(_request):
        raise error

    fake.route("GET", GEOCODE_URL, handler=boom)
    with caplog.at_level(logging.DEBUG):
        assert await _lookup() is None
    assert any(type(error).__name__ in r.getMessage() for r in caplog.records)
    _no_position_or_address_logged(caplog)


async def test_a_successful_lookup_logs_nothing_with_the_address(fake, caplog):
    fake.route("GET", GEOCODE_URL, 200, OK)
    with caplog.at_level(logging.DEBUG):
        assert await _lookup() == ADDRESS
    _no_position_or_address_logged(caplog)
