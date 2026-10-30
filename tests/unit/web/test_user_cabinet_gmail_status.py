"""
/api/gmail/status exposes needs_reconnect — before it, a revoked Gmail grant
still read as a healthy "connected" in the Cabinet (incident 2026-10-09).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_USER_ID = "user-1"


def _app(creds):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(
        return_value={"sub": _USER_ID, "account_id": "account-1", "role": "owner"}
    )
    oauth = MagicMock()
    oauth.list_for_user = AsyncMock(return_value=[creds])
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(),
        session_service=session_service,
        user_repo=MagicMock(),
        fact_repo=MagicMock(),
        embedding_service=MagicMock(),
        oauth_credentials_port=oauth, connectors=ConnectorRegistry(),
    )
    app = Quart("test_app")
    app.register_blueprint(bp)
    return app


def _creds(flag: bool) -> OAuthCredential:
    return OAuthCredential(
        user_id=_USER_ID,
        account_id="acct-1",
        kind="gmail",
        integration=Integration.MAIL,
        account=ProviderAccount(realm="google", subject="", label="user@example.com"),
        access_token="tok",
        refresh_token="rtok",
        token_expiry=datetime(2026, 10, 10, tzinfo=timezone.utc),
        scopes=[],
        needs_reconnect=flag,
    )


async def _status(creds):
    async with _app(creds).test_client() as client:
        resp = await client.get("/api/gmail/status", headers={"Authorization": "Bearer t"})
    assert resp.status_code == 200
    return await resp.get_json()


async def test_revoked_grant_reports_needs_reconnect():
    body = await _status(_creds(True))
    assert body["connected"] is True
    assert body["needs_reconnect"] is True


async def test_healthy_grant_reports_no_reconnect():
    body = await _status(_creds(False))
    assert body["needs_reconnect"] is False
