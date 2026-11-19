"""ReverseGeocodingPort — the nearest street address, landmarks and areas of a position (GEO_MARKS_RFC §4.3).

A system boundary (Google Geocoding) with a test double. A mark without an address is a valid mark, so the port never
raises: every failure reads as `GeoPlace.EMPTY`.
"""
from abc import ABC, abstractmethod

from src.domain.geo_mark import GeoPlace


class ReverseGeocodingPort(ABC):

    @abstractmethod
    async def look_up(self, latitude: float, longitude: float) -> GeoPlace:
        """The nearest street address, the landmarks and the areas around the position (names in English), or
        `GeoPlace.EMPTY`. Never raises."""
