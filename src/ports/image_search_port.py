"""
ImageSearchPort — system boundary for stock-photo search services.

Implementations: UnsplashAdapter (src/adapters/unsplash_adapter.py).
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageResult:
    """A single photo result from an image search service."""
    url: str           # Regular size (~1080px wide) — fallback
    raw_url: str       # Original file URL — use with ?w=&h=&fit=crop for exact sizing
    photographer: str
    photographer_url: str


class ImageSearchPort(ABC):
    """Query a stock-photo service and return relevant image URLs."""

    @abstractmethod
    async def search(self, query: str, count: int = 1) -> list[ImageResult]:
        """
        Search for images matching the query.

        Returns up to `count` results. Returns [] on any error.
        """
