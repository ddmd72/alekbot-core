"""
Unit tests for the MCP OAuth consent blueprint.

The blueprint is tiny — two routes — so tests cover the three paths the
SDK actually exercises:
    1. GET /mcp/consent without Cabinet cookie → 302 to /auth/login
    2. GET /mcp/consent with Cabinet cookie + valid req JWT → 200 HTML
    3. POST /mcp/consent action=approve → 302 to claude callback with code
    4. POST /mcp/consent action=deny → 302 to claude callback with error
    5. POST /mcp/consent with bad cookie → 302 to /auth/login
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from quart import Quart

from src.services.mcp_authorization_service import (
    AuthorizationParams,
    ConsentRequest,
    MCPAuthError,
)
from src.web.mcp_consent_app import build_mcp_consent_blueprint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_mcp_service():
    svc = Mock()
    svc.verify_consent_request = Mock(
        return_value=ConsentRequest(
            client_id="mcp-claude-1",
            redirect_uri="https://claude.ai/api/mcp/auth_callback",
            code_challenge="c" * 43,
            code_challenge_method="S256",
            resource="https://dev.alekbot.app/mcp",
            scopes=["user_context"],
            state="xyz",
        )
    )
    svc.get_client = AsyncMock(
        return_value=Mock(client_name="Claude")
    )
    svc.issue_auth_code_for_consent = AsyncMock(
        return_value=(
            "the-code",
            ConsentRequest(
                client_id="mcp-claude-1",
                redirect_uri="https://claude.ai/api/mcp/auth_callback",
                code_challenge="c" * 43,
                code_challenge_method="S256",
                resource="https://dev.alekbot.app/mcp",
                scopes=["user_context"],
                state="xyz",
            ),
        )
    )
    return svc


@pytest.fixture
def mock_session_service():
    svc = Mock()
    svc.verify_access_token = Mock(
        return_value={"sub": "user-1", "account_id": "acc-1"}
    )
    return svc


@pytest.fixture
def app(mock_mcp_service, mock_session_service):
    app = Quart("test_app")
    app.register_blueprint(
        build_mcp_consent_blueprint(mock_mcp_service, mock_session_service)
    )
    return app


# ---------------------------------------------------------------------------
# GET /mcp/consent
# ---------------------------------------------------------------------------


class TestGetConsent:
    async def test_missing_req_returns_400(self, app):
        async with app.test_client() as client:
            resp = await client.get("/mcp/consent")
            assert resp.status_code == 400

    async def test_no_cookie_redirects_to_login(self, app):
        async with app.test_client() as client:
            resp = await client.get("/mcp/consent?req=JWT")
            assert resp.status_code == 302
            assert "/auth/login" in resp.headers["Location"]

    async def test_valid_cookie_renders_consent_page(
        self, app, mock_mcp_service, mock_session_service
    ):
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.get("/mcp/consent?req=JWT")
            assert resp.status_code == 200
            body = (await resp.get_data()).decode()
            assert "Claude" in body
            assert "user_context" in body
            assert "Approve" in body
            assert "Deny" in body

    async def test_bad_jwt_returns_400(
        self, app, mock_mcp_service, mock_session_service
    ):
        mock_mcp_service.verify_consent_request = Mock(
            side_effect=MCPAuthError("bad jwt")
        )
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.get("/mcp/consent?req=bad")
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /mcp/consent
# ---------------------------------------------------------------------------


class TestPostConsent:
    async def test_approve_issues_code_and_redirects(
        self, app, mock_mcp_service
    ):
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.post(
                "/mcp/consent",
                form={"req": "JWT", "action": "approve"},
            )
            assert resp.status_code == 302
            assert "https://claude.ai/api/mcp/auth_callback" in resp.headers["Location"]
            assert "code=the-code" in resp.headers["Location"]
            assert "state=xyz" in resp.headers["Location"]
            mock_mcp_service.issue_auth_code_for_consent.assert_awaited_once_with(
                "JWT", user_id="user-1", account_id="acc-1"
            )

    async def test_deny_redirects_with_access_denied(self, app):
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.post(
                "/mcp/consent",
                form={"req": "JWT", "action": "deny"},
            )
            assert resp.status_code == 302
            assert "https://claude.ai/api/mcp/auth_callback" in resp.headers["Location"]
            assert "error=access_denied" in resp.headers["Location"]
            assert "state=xyz" in resp.headers["Location"]

    async def test_no_cookie_redirects_to_login(self, app):
        async with app.test_client() as client:
            resp = await client.post(
                "/mcp/consent",
                form={"req": "JWT", "action": "approve"},
            )
            assert resp.status_code == 302
            assert "/auth/login" in resp.headers["Location"]

    async def test_unknown_action_returns_400(self, app):
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.post(
                "/mcp/consent",
                form={"req": "JWT", "action": "whatever"},
            )
            assert resp.status_code == 400

    async def test_missing_form_fields_returns_400(self, app):
        async with app.test_client() as client:
            client.set_cookie("localhost", "access_token", "valid")
            resp = await client.post("/mcp/consent", form={})
            assert resp.status_code == 400
