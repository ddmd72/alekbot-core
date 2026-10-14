"""
The MCP consent page renders every value as text, never as markup, and is served with headers that keep it out of
frames and away from other origins' resources.

Client registration is anonymous (RFC 7591 DCR), so the client name shown on this page is chosen by whoever
registered the client; the scopes, the request token and the error text also reach the HTML.
"""

from unittest.mock import AsyncMock, Mock

import pytest
from quart import Quart

from src.services.mcp_authorization_service import ConsentRequest
from src.web.mcp_consent_app import build_mcp_consent_blueprint

_MARKUP = '<script>alert(1)</script><img src=x onerror="alert(2)">'


def _consent(scopes):
    return ConsentRequest(
        client_id="mcp-x",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        code_challenge="c" * 43,
        code_challenge_method="S256",
        resource="https://dev.alekbot.app/mcp",
        scopes=scopes,
        state="s",
    )


def _app(client_name="Claude", scopes=("user_context",)):
    svc = Mock()
    svc.verify_consent_request = Mock(return_value=_consent(list(scopes)))
    svc.get_client = AsyncMock(return_value=Mock(client_name=client_name))
    sessions = Mock()
    sessions.verify_access_token = Mock(return_value={"sub": "user-1", "account_id": "acc-1"})
    app = Quart("test_consent_rendering")
    app.register_blueprint(build_mcp_consent_blueprint(svc, sessions))
    return app


async def _page(app, req="JWT"):
    async with app.test_client() as client:
        client.set_cookie("localhost", "access_token", "valid")
        resp = await client.get("/mcp/consent", query_string={"req": req})
        return resp, (await resp.get_data()).decode()


async def test_client_name_is_text_not_markup():
    resp, body = await _page(_app(client_name=_MARKUP))
    assert resp.status_code == 200
    assert "<script>" not in body and "<img" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


async def test_scopes_are_text_not_markup():
    _, body = await _page(_app(scopes=[_MARKUP]))
    assert "<script>" not in body and "<img" not in body
    assert "&lt;script&gt;" in body


async def test_request_token_cannot_leave_its_attribute():
    _, body = await _page(_app(), req='JWT"><script>alert(3)</script>')
    assert "<script>" not in body
    assert 'value="JWT&quot;&gt;&lt;script&gt;alert(3)&lt;/script&gt;"' in body


async def test_page_carries_security_headers():
    resp, _ = await _page(_app())
    csp = resp.headers["Content-Security-Policy"]
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"):
        assert directive in csp
    assert "script-src" not in csp            # no script on this page at all: default-src 'none' covers it
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/mcp/consent?req=JWT&x=1"])
async def test_login_redirect_encodes_the_return_url(path):
    async with _app().test_client() as client:
        resp = await client.get(path)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.startswith("/auth/login?next=")
    assert "%26x%3D1" in location             # the consent URL's own query is one encoded value
