"""Public landing at `/`: static page without a session, Cabinet redirect with one."""
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.web.landing_app import create_landing_blueprint


def _app(token_valid: bool = False):
    session_service = MagicMock()
    if not token_valid:
        session_service.verify_access_token = MagicMock(side_effect=ValueError("bad token"))
    app = Quart(__name__)
    app.register_blueprint(create_landing_blueprint(session_service))
    return app


@pytest.mark.asyncio
async def test_root_without_session_serves_the_landing_page_not_the_login():
    resp = await _app().test_client().get("/")

    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    body = (await resp.get_data()).decode()
    assert "Alek-Core" in body
    assert 'href="/auth/login"' in body
    assert resp.headers["Cache-Control"] == "no-cache"


@pytest.mark.asyncio
async def test_root_with_valid_login_cookie_redirects_to_cabinet():
    client = _app(token_valid=True).test_client()
    client.set_cookie("localhost", "access_token", "jwt")

    resp = await client.get("/")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/cabinet")


@pytest.mark.asyncio
async def test_root_with_invalid_cookie_shows_the_landing_page():
    client = _app(token_valid=False).test_client()
    client.set_cookie("localhost", "access_token", "expired")

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Alek-Core" in (await resp.get_data()).decode()


@pytest.mark.asyncio
async def test_share_image_is_a_public_png():
    resp = await _app().test_client().get("/og.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert (await resp.get_data())[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_robots_opens_only_the_root():
    resp = await _app().test_client().get("/robots.txt")

    text = (await resp.get_data()).decode()
    assert "Allow: /$" in text
    assert "Disallow: /\n" in text


def test_landing_page_has_no_placeholders_or_external_assets():
    from pathlib import Path
    html = Path("src/web/static/landing.html").read_text()

    assert "data-todo" not in html and 'href="#"' not in html
    assert "<script src" not in html and "fonts.googleapis" not in html


def test_every_external_link_opens_in_a_new_tab():
    import re
    from pathlib import Path
    html = Path("src/web/static/landing.html").read_text()
    anchors = re.findall(r"<a [^>]*href=\"https://[^>]*>", html)

    assert anchors
    assert all('target="_blank"' in a and "noopener" in a for a in anchors)
