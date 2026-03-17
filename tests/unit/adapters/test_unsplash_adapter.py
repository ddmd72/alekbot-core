"""
Wire tests for UnsplashAdapter.

Mock boundary: aiohttp.ClientSession (HTTP layer).
Never mock at ImageSearchPort level — that hides translation bugs.

Covers:
- Constructor stores access key
- Success path: ImageResult fields mapped correctly from API response
- photographer_url includes UTM params
- raw_url passed through from urls.raw
- count param: sent as per_page, clamped to [1, 10]
- query truncated to 200 chars before sending
- orientation=landscape always set
- Multiple results: only first `count` items returned
- Non-200 response → returns []
- Empty results list in API response → returns []
- Connection error (aiohttp exception) → returns []
- Timeout exception → returns []
- JSON parse failure → returns []
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.unsplash_adapter import UnsplashAdapter, _UNSPLASH_SEARCH_URL
from src.ports.image_search_port import ImageResult, ImageSearchPort


# =============================================================================
# Helpers
# =============================================================================

def _make_photo(
    url="https://images.unsplash.com/photo-1?w=1080",
    raw_url="https://images.unsplash.com/photo-1",
    photographer_name="Jane Smith",
    photographer_html="https://unsplash.com/@janesmith",
):
    return {
        "urls": {
            "regular": url,
            "raw": raw_url,
        },
        "user": {
            "name": photographer_name,
            "links": {"html": photographer_html},
        },
    }


def _make_response(photos: list, status: int = 200):
    """Build a fake aiohttp response that returns photos in results[]."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value={"results": photos})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(resp):
    """Wrap response in a fake aiohttp.ClientSession context manager."""
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _patch_session(session):
    return patch("aiohttp.ClientSession", return_value=session)


# =============================================================================
# Port compliance
# =============================================================================

class TestUnsplashAdapterPortCompliance:

    def test_is_image_search_port_subclass(self):
        assert issubclass(UnsplashAdapter, ImageSearchPort)

    def test_instantiates_with_access_key(self):
        adapter = UnsplashAdapter(access_key="test-key-123")
        assert isinstance(adapter, UnsplashAdapter)

    def test_stores_access_key(self):
        adapter = UnsplashAdapter(access_key="my-secret-key")
        assert adapter._access_key == "my-secret-key"


# =============================================================================
# Success path — field mapping
# =============================================================================

class TestUnsplashAdapterSuccess:

    async def test_returns_list_of_image_results(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountains fog", count=1)

        assert len(results) == 1
        assert isinstance(results[0], ImageResult)

    async def test_url_mapped_from_regular(self):
        photo = _make_photo(url="https://images.unsplash.com/photo-abc?w=1080")
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("forest", count=1)

        assert results[0].url == "https://images.unsplash.com/photo-abc?w=1080"

    async def test_raw_url_mapped_from_raw(self):
        photo = _make_photo(raw_url="https://images.unsplash.com/photo-abc")
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("forest", count=1)

        assert results[0].raw_url == "https://images.unsplash.com/photo-abc"

    async def test_photographer_name_mapped(self):
        photo = _make_photo(photographer_name="Alice Kim")
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("city", count=1)

        assert results[0].photographer == "Alice Kim"

    async def test_photographer_url_includes_utm_params(self):
        photo = _make_photo(photographer_html="https://unsplash.com/@alice")
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("city", count=1)

        assert "utm_source=alekbot" in results[0].photographer_url
        assert "utm_medium=referral" in results[0].photographer_url

    async def test_photographer_url_base_preserved(self):
        photo = _make_photo(photographer_html="https://unsplash.com/@bob")
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("ocean", count=1)

        assert results[0].photographer_url.startswith("https://unsplash.com/@bob")

    async def test_empty_results_returns_empty_list(self):
        session = _make_session(_make_response([]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("noresults", count=1)

        assert results == []

    async def test_multiple_photos_count_respected(self):
        photos = [
            _make_photo(url=f"https://images.unsplash.com/photo-{i}?w=1080")
            for i in range(5)
        ]
        session = _make_session(_make_response(photos))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("landscape", count=3)

        assert len(results) == 3

    async def test_count_one_returns_one_result(self):
        photos = [_make_photo(), _make_photo(url="https://images.unsplash.com/photo-2")]
        session = _make_session(_make_response(photos))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountain", count=1)

        assert len(results) == 1


# =============================================================================
# Request params sent to Unsplash API
# =============================================================================

class TestUnsplashAdapterRequestParams:

    async def test_authorization_header_uses_client_id(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="my-access-key")

        with _patch_session(session):
            await adapter.search("mountains", count=1)

        call_kwargs = session.get.call_args.kwargs
        assert call_kwargs["headers"] == {"Authorization": "Client-ID my-access-key"}

    async def test_request_url_is_search_endpoint(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("mountains", count=1)

        called_url = session.get.call_args.args[0]
        assert called_url == _UNSPLASH_SEARCH_URL

    async def test_query_sent_as_param(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("sunset beach", count=1)

        params = session.get.call_args.kwargs["params"]
        assert params["query"] == "sunset beach"

    async def test_orientation_always_landscape(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("portrait photo", count=1)

        params = session.get.call_args.kwargs["params"]
        assert params["orientation"] == "landscape"

    async def test_per_page_matches_count(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("mountains", count=4)

        params = session.get.call_args.kwargs["params"]
        assert params["per_page"] == 4

    async def test_count_clamped_to_minimum_one(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("mountains", count=0)

        params = session.get.call_args.kwargs["params"]
        assert params["per_page"] >= 1

    async def test_count_clamped_to_maximum_ten(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("mountains", count=99)

        params = session.get.call_args.kwargs["params"]
        assert params["per_page"] <= 10

    async def test_query_truncated_to_200_chars(self):
        long_query = "a" * 300
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search(long_query, count=1)

        params = session.get.call_args.kwargs["params"]
        assert len(params["query"]) <= 200

    async def test_short_query_not_truncated(self):
        photo = _make_photo()
        session = _make_session(_make_response([photo]))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            await adapter.search("mountain lake", count=1)

        params = session.get.call_args.kwargs["params"]
        assert params["query"] == "mountain lake"


# =============================================================================
# Failure paths — graceful degradation
# =============================================================================

class TestUnsplashAdapterFailurePaths:

    async def test_non_200_returns_empty_list(self):
        session = _make_session(_make_response([], status=401))
        adapter = UnsplashAdapter(access_key="bad-key")

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_403_returns_empty_list(self):
        session = _make_session(_make_response([], status=403))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_500_returns_empty_list(self):
        session = _make_session(_make_response([], status=500))
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_connection_error_returns_empty_list(self):
        adapter = UnsplashAdapter(access_key="key")

        with patch("aiohttp.ClientSession", side_effect=Exception("Connection refused")):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_timeout_returns_empty_list(self):
        import asyncio
        adapter = UnsplashAdapter(access_key="key")

        session = MagicMock()
        session.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        session.__aexit__ = AsyncMock(return_value=False)

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_json_decode_error_returns_empty_list(self):
        import json
        resp = _make_response([])
        resp.json = AsyncMock(side_effect=json.JSONDecodeError("bad json", "", 0))
        session = _make_session(resp)
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []

    async def test_missing_results_key_returns_empty_list(self):
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value={})  # no "results" key
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        session = _make_session(resp)
        adapter = UnsplashAdapter(access_key="key")

        with _patch_session(session):
            results = await adapter.search("mountains", count=1)

        assert results == []
