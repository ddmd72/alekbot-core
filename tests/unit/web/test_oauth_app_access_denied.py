"""
OAuth web handlers on a whitelist denial (AccessDeniedError).

/auth/callback must answer 403 with no session cookies; /auth/link-oauth must
answer 403 (not the 400 every other ValueError gets).

Decision: docs/04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md
"""
from unittest.mock import AsyncMock, Mock

import pytest
from quart import Quart

from src.domain.exceptions import AccessDeniedError
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE
from src.web.oauth_app import create_oauth_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
from src.composition.connector_registry import ConnectorRegistry


@pytest.fixture
def auth_service():
    svc = Mock()
    svc.handle_oauth_callback = AsyncMock(side_effect=AccessDeniedError("not in whitelist"))
    svc.link_oauth_identity = AsyncMock(side_effect=AccessDeniedError("not in whitelist"))
    return svc


@pytest.fixture
def kit():
    return CabinetSessionKit()


@pytest.fixture
def app(auth_service, kit):
    auth_config = Mock()
    auth_config.oauth_redirect_uri = "https://example.test/auth/callback"
    app = Quart("test_app")
    app.register_blueprint(
        create_oauth_blueprint(
            auth_service=auth_service,
            cabinet_sessions=kit.verifier,
            device_sessions=kit.device_sessions,
            auth_registry=Mock(),
            auth_config=auth_config, connectors=ConnectorRegistry(),
        )
    )
    return app


async def test_callback_denied_returns_403_without_session_cookies(app, kit):
    async with app.test_client() as client:
        client.set_cookie("localhost", "oauth_state", "s1")
        resp = await client.get("/auth/callback?code=c&state=s1")

    assert resp.status_code == 403
    assert "text/html" in resp.headers["Content-Type"]
    set_cookies = " ".join(resp.headers.get_all("Set-Cookie"))
    assert f"{ACCESS_COOKIE}=" not in set_cookies
    assert f"{REFRESH_COOKIE}=" not in set_cookies
    assert kit.repo.sessions == {}          # no device session was started, so no token of either kind exists


async def test_callback_denied_does_not_leak_reason(app):
    async with app.test_client() as client:
        client.set_cookie("localhost", "oauth_state", "s1")
        resp = await client.get("/auth/callback?code=c&state=s1")

    body = await resp.get_data(as_text=True)
    assert "whitelist" not in body.lower()


async def test_link_oauth_denied_returns_403(app, kit):
    async with app.test_client() as client:
        client.set_cookie("localhost", "oauth_state", "s1")
        kit.set_cookies(client, kit.sign_in_now("u1", "a1"), refresh=False)
        resp = await client.post("/auth/link-oauth", json={"code": "c", "state": "s1"})

    assert resp.status_code == 403
