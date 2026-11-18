"""GoogleGeocodingAdapter — reverse geocoding over the Google Geocoding API (GEO_MARKS_RFC §4.3).

`GET https://maps.googleapis.com/maps/api/geocode/json?latlng=<lat>,<lon>&language=<ui language>&key=<key>`, one
aiohttp request under a total timeout; the first result's `formatted_address`. Any other status, an HTTP error, an
unreadable body or the timeout reads as no address: the adapter never raises, and a failure never fails a mark.

Logs carry the API status or the error class only: never the coordinates, the address, or the request URL (it holds
the key).
"""
from typing import Any, Optional

import aiohttp

from ..ports.reverse_geocoding_port import ReverseGeocodingPort
from ..utils.logger import logger

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"


class GoogleGeocodingAdapter(ReverseGeocodingPort):

    def __init__(self, api_key: str, *, timeout_s: float = 3.0) -> None:
        self._api_key = api_key
        self._timeout_s = timeout_s

    async def nearest_address(self, latitude: float, longitude: float, *, language: str) -> Optional[str]:
        params = {"latlng": f"{latitude},{longitude}", "language": language, "key": self._api_key}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(GEOCODE_URL, params=params,
                                       timeout=aiohttp.ClientTimeout(total=self._timeout_s)) as resp:
                    if resp.status != 200:
                        logger.warning("[Geocoding] HTTP %s", resp.status)
                        return None
                    body: Any = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("[Geocoding] request failed: %s", type(exc).__name__)
            return None
        return _first_address(body)


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
