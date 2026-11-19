"""ReverseGeocodingPort — the nearest street address of a position (GEO_MARKS_RFC §4.3).

A system boundary (Google Geocoding) with a test double. A mark without an address is a valid mark, so the port never
raises: every failure reads as "no address".
"""
from abc import ABC, abstractmethod
from typing import Optional


class ReverseGeocodingPort(ABC):

    @abstractmethod
    async def nearest_address(self, latitude: float, longitude: float, *, language: str) -> Optional[str]:
        """The nearest street address, or None. Never raises."""
