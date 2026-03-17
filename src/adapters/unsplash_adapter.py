"""
UnsplashAdapter — ImageSearchPort implementation backed by the Unsplash API.

Requires an Unsplash Client-ID (access key) passed via constructor.
Read from UNSPLASH_ACCESS_KEY env var at startup in composition/user_agent_factory.py.

Returns [] silently on any request failure.
"""
import aiohttp

from ..ports.image_search_port import ImageResult, ImageSearchPort
from ..utils.logger import logger

_UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
_TIMEOUT_SECONDS = 5


class UnsplashAdapter(ImageSearchPort):
    """Fetches photos from Unsplash /search/photos."""

    def __init__(self, access_key: str) -> None:
        self._access_key = access_key

    async def search(self, query: str, count: int = 1) -> list[ImageResult]:
        headers = {"Authorization": f"Client-ID {self._access_key}"}
        params = {
            "query": query[:200],
            "per_page": min(max(count, 1), 10),
            "orientation": "landscape",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    _UNSPLASH_SEARCH_URL, params=params, headers=headers
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            "UnsplashAdapter: API returned %d for query %r",
                            resp.status, query[:60],
                        )
                        return []
                    data = await resp.json()
                    results = []
                    for photo in data.get("results", [])[:count]:
                        photographer_url = (
                            photo["user"]["links"]["html"]
                            + "?utm_source=alekbot&utm_medium=referral"
                        )
                        results.append(ImageResult(
                            url=photo["urls"]["regular"],
                            raw_url=photo["urls"]["raw"],
                            photographer=photo["user"]["name"],
                            photographer_url=photographer_url,
                        ))
                    logger.info(
                        "UnsplashAdapter: fetched %d images for %r",
                        len(results), query[:60],
                    )
                    return results
        except Exception as exc:
            logger.warning("UnsplashAdapter: request failed: %s", exc)
            return []
