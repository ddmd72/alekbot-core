"""
Unit tests for the /s/<code> short-link blueprint.

Covers: valid code → 302 to target; missing/expired code → 404;
malformed code → 404 without ever calling the resolver (defends the
Firestore-read cost from garbage/scanner traffic).
"""
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.short_link_app import create_short_link_blueprint

_TARGET = "https://dev.alekbot.app/f/some-long-token"


def _app(short_links):
    app = Quart("test_app")
    app.register_blueprint(create_short_link_blueprint(short_links=short_links))
    return app


class TestValidCode:

    async def test_valid_code_redirects_to_target(self):
        short_links = MagicMock()
        short_links.resolve = AsyncMock(return_value=_TARGET)
        app = _app(short_links)
        async with app.test_client() as client:
            resp = await client.get("/s/abc1234567")
        assert resp.status_code == 302
        assert resp.headers["Location"] == _TARGET
        short_links.resolve.assert_awaited_once_with("abc1234567")


class TestMissingOrExpired:

    async def test_unknown_code_404(self):
        short_links = MagicMock()
        short_links.resolve = AsyncMock(return_value=None)
        app = _app(short_links)
        async with app.test_client() as client:
            resp = await client.get("/s/abc1234567")
        assert resp.status_code == 404


class TestMalformedCode:

    async def test_wrong_length_404_without_resolving(self):
        short_links = MagicMock()
        short_links.resolve = AsyncMock(return_value=_TARGET)
        app = _app(short_links)
        async with app.test_client() as client:
            resp = await client.get("/s/short")
        assert resp.status_code == 404
        short_links.resolve.assert_not_called()

    async def test_non_alnum_char_404_without_resolving(self):
        short_links = MagicMock()
        short_links.resolve = AsyncMock(return_value=_TARGET)
        app = _app(short_links)
        async with app.test_client() as client:
            resp = await client.get("/s/abc123456!")
        assert resp.status_code == 404
        short_links.resolve.assert_not_called()
