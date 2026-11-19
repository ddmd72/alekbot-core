"""GoogleGeocodingAdapter — reverse geocoding over the Google Geocoding API (GEO_MARKS_RFC §4.3).

`GET https://maps.googleapis.com/maps/api/geocode/json?latlng=<lat>,<lon>&language=en&extra_computations=ADDRESS_DESCRIPTORS&key=<key>`,
one aiohttp request under a total timeout. The result is a `GeoPlace`: the first result's `formatted_address`, and
the response-level `address_descriptor`'s landmarks (up to 5) and areas (up to 3), in the API's order. Names are
requested in English so a local name is never transliterated. A missing or malformed descriptor gives no landmarks
and keeps the address. Any other status, an HTTP error, an unreadable body or the timeout reads as
`GeoPlace.EMPTY`: the adapter never raises, and a failure never fails a mark.

Logs carry the API status or the error class only: never the coordinates, the address, a place name, or the request
URL (it holds the key).
"""
from typing import Any, List, Optional, Tuple

import aiohttp

from ..domain.geo_mark import GeoArea, GeoLandmark, GeoPlace
from ..ports.reverse_geocoding_port import ReverseGeocodingPort
from ..utils.logger import logger

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
MAX_LANDMARKS = 5
MAX_AREAS = 3
_GENERIC_KINDS = frozenset({"establishment", "point_of_interest"})
_CONTAINMENTS = frozenset({"WITHIN", "OUTSKIRTS", "NEAR"})
_RELATIONS = frozenset({"NEAR", "WITHIN", "BESIDE", "BEHIND", "ACROSS_THE_ROAD", "DOWN_THE_ROAD", "AROUND_THE_CORNER"})


class GoogleGeocodingAdapter(ReverseGeocodingPort):

    def __init__(self, api_key: str, *, timeout_s: float = 3.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def look_up(self, latitude: float, longitude: float) -> GeoPlace:
        params = {"latlng": f"{latitude},{longitude}", "language": "en",
                  "extra_computations": "ADDRESS_DESCRIPTORS", "key": self._api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(GEOCODE_URL, params=params,
                                       timeout=aiohttp.ClientTimeout(total=self._timeout_s)) as resp:
                    if resp.status != 200:
                        logger.warning("[Geocoding] HTTP %s", resp.status)
                        return GeoPlace.EMPTY
                    body: Any = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("[Geocoding] request failed: %s", type(exc).__name__)
            return GeoPlace.EMPTY
        address = _first_address(body)
        if not (isinstance(body, dict) and body.get("status") == "OK"):
            return GeoPlace.EMPTY
        landmarks: Tuple[GeoLandmark, ...] = ()
        areas: Tuple[GeoArea, ...] = ()
        try:
            descriptor = body.get("address_descriptor") if isinstance(body, dict) else None
            if descriptor is not None:
                landmarks, areas = _parse_descriptor(descriptor)
        except Exception as exc:
            logger.warning("[Geocoding] malformed address_descriptor: %s", type(exc).__name__)
            landmarks, areas = (), ()
        return GeoPlace(address, landmarks, areas)


def _first_address(body: Any) -> Optional[str]:
    status = body.get("status") if isinstance(body, dict) else None
    if status != "OK":
        logger.warning("[Geocoding] status=%s", status if isinstance(status, str) else "unreadable")
        return None
    results = body.get("results")
    first = results[0] if isinstance(results, list) and results else None
    address = first.get("formatted_address") if isinstance(first, dict) else None
    if not isinstance(address, str) or not address.strip():
        logger.warning("[Geocoding] status=OK without a formatted address")
        return None
    return address.strip()


def _name(entry: Any) -> Optional[str]:
    display = entry.get("display_name") if isinstance(entry, dict) else None
    text = display.get("text") if isinstance(display, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def _kinds(types: Any) -> Tuple[str, ...]:
    if not isinstance(types, list):
        return ()
    return tuple(t.replace("_", " ") for t in types if isinstance(t, str) and t not in _GENERIC_KINDS)


def _landmark(entry: Any) -> Optional[GeoLandmark]:
    name = _name(entry)
    distance = entry.get("straight_line_distance_meters") if isinstance(entry, dict) else None
    if name is None or isinstance(distance, bool) or not isinstance(distance, (int, float)):
        return None
    relation = entry.get("spatial_relationship")
    return GeoLandmark(name, _kinds(entry.get("types")),
                       relation if isinstance(relation, str) and relation in _RELATIONS else None, int(round(distance)))


def _area(entry: Any) -> Optional[GeoArea]:
    name = _name(entry)
    containment = entry.get("containment") if isinstance(entry, dict) else None
    if name is None or not isinstance(containment, str) or containment not in _CONTAINMENTS:
        return None
    return GeoArea(name, containment)


def _parse_descriptor(descriptor: Any) -> Tuple[Tuple[GeoLandmark, ...], Tuple[GeoArea, ...]]:
    """Raises on a descriptor that is not an object or whose lists are not lists (the caller drops both)."""
    if not isinstance(descriptor, dict):
        raise ValueError("descriptor is not an object")
    raw_landmarks = descriptor.get("landmarks", [])
    raw_areas = descriptor.get("areas", [])
    if not isinstance(raw_landmarks, list) or not isinstance(raw_areas, list):
        raise ValueError("descriptor lists are not lists")
    landmarks: List[GeoLandmark] = [x for x in map(_landmark, raw_landmarks) if x is not None]
    areas: List[GeoArea] = [x for x in map(_area, raw_areas) if x is not None]
    landmarks.sort(key=lambda x: x.distance_m)      # Google ranks by prominence; the port returns nearest first
    return tuple(landmarks[:MAX_LANDMARKS]), tuple(areas[:MAX_AREAS])
