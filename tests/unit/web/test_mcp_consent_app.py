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
from tests.support.cabinet_session_kit import CabinetSessionKit

_KIT = CabinetSessionKit()


def _sign_in(client) -> None:
    """The consenting user's Cabinet session: a live device session's access cookie."""
    _KIT.set_cookies(client, _KIT.sign_in_now("user-1", "acc-1"), refresh=False)


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
def app(mock_mcp_service):
    app = Quart("test_app")
    app.register_blueprint(
        build_mcp_consent_blueprint(mock_mcp_service, _KIT.verifier)
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
        self, app, mock_mcp_service
    ):
        async with app.test_client() as client:
            _sign_in(client)
            resp = await client.get("/mcp/consent?req=JWT")
            assert resp.status_code == 200
            body = (await resp.get_data()).decode()
            assert "Claude" in body
            assert "user_context" in body
            assert "Approve" in body
            assert "Deny" in body

    async def test_bad_jwt_returns_400(
        self, app, mock_mcp_service
    ):
        mock_mcp_service.verify_consent_request = Mock(
            side_effect=MCPAuthError("bad jwt")
        )
        async with app.test_client() as client:
            _sign_in(client)
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
            _sign_in(client)
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
            _sign_in(client)
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
            _sign_in(client)
            resp = await client.post(
                "/mcp/consent",
                form={"req": "JWT", "action": "whatever"},
            )
            assert resp.status_code == 400

    async def test_missing_form_fields_returns_400(self, app):
        async with app.test_client() as client:
            _sign_in(client)
            resp = await client.post("/mcp/consent", form={})
            assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The memory inbox grant (MCP_MEMORY_SUBMISSIONS_RFC §5.1, plan §15.5)
# ---------------------------------------------------------------------------


async def test_an_inbox_grant_gets_its_own_consent_text(mock_mcp_service):
    mock_mcp_service.verify_consent_request = Mock(return_value=ConsentRequest(
        client_id="mcp-claude-1", redirect_uri="https://claude.ai/api/mcp/auth_callback", code_challenge="c" * 43,
        code_challenge_method="S256", resource="https://dev.alekbot.app/memory-inbox-mcp", scopes=["memory_submit"],
        state="xyz"))
    mock_mcp_service.get_client = AsyncMock(return_value=Mock(client_name="<b>Desk</b>"))
    app = Quart("test_inbox_consent")
    app.register_blueprint(build_mcp_consent_blueprint(mock_mcp_service, _KIT.verifier))
    async with app.test_client() as client:
        _sign_in(client)
        resp = await client.get("/mcp/consent?req=JWT")
        body = (await resp.get_data()).decode()
    assert resp.status_code == 200
    assert "Authorize memory inbox connector" in body
    assert "<strong>&lt;b&gt;Desk&lt;/b&gt;</strong> is asking to send records about you" in body
    assert "<code>memory_submit</code>" in body and "Approve" in body
    assert 'name="worker_id"' not in body and "biographical facts" not in body
    assert "Authorize MCP connector" not in body and "Authorize task connector" not in body
