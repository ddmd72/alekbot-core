"""A failed Graph call logs its status and enum-like codes, never the response body's free text (hardening, §27.12)."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.drive_handles import drive_handle
from tests.support.oauth_credentials_store import mirror_get

MARKER = "SECRET-TRANSCRIPT-MARKER"


def _resp(body, status):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=body)
    r.text = AsyncMock(return_value=json.dumps(body))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(**methods):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    for name, value in methods.items():
        getattr(s, name).side_effect = value
    return s


def _oauth():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = OAuthCredential(
        user_id="u1",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="tok",
        refresh_token="r",
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        scopes=[],
    )
    mirror_get(oauth)
    return oauth


async def test_a_token_refresh_failure_logs_status_and_error_code_only(caplog):
    body = {"error": "temporarily_unavailable", "error_description": f"AADSTS90033 {MARKER}"}
    provider = MicrosoftGraphTokenProvider(_oauth(), "cid", "csecret", ONEDRIVE_PROVIDER, "scope")
    with patch("aiohttp.ClientSession", return_value=_session(post=[_resp(body, 503)])), \
            pytest.raises(ValueError) as exc:
        await provider.user_headers("u1")
    assert MARKER not in caplog.text and MARKER not in str(exc.value)
    assert "503" in caplog.text and "temporarily_unavailable" in caplog.text


async def test_a_free_text_error_value_is_not_logged_as_a_code(caplog):
    body = {"error": f"{MARKER} with spaces", "error_description": "x"}
    provider = MicrosoftGraphTokenProvider(_oauth(), "cid", "csecret", ONEDRIVE_PROVIDER, "scope")
    with patch("aiohttp.ClientSession", return_value=_session(post=[_resp(body, 400)])), pytest.raises(ValueError):
        await provider.user_headers("u1")
    assert MARKER not in caplog.text and ", 400)" in caplog.text


async def test_a_not_json_token_error_logs_the_status_only(caplog):
    r = _resp({}, 502)
    r.text = AsyncMock(return_value=f"<html>{MARKER}</html>")
    provider = MicrosoftGraphTokenProvider(_oauth(), "cid", "csecret", ONEDRIVE_PROVIDER, "scope")
    with patch("aiohttp.ClientSession", return_value=_session(post=[r])), pytest.raises(ValueError):
        await provider.user_headers("u1")
    assert MARKER not in caplog.text and "502" in caplog.text


async def test_a_drive_call_failure_logs_status_and_graph_codes_only(caplog):
    oauth = _oauth()
    oauth.get.return_value.token_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    body = {"error": {"code": "generalException", "message": f"{MARKER}.md", "innerError": {"code": "inner"}}}
    adapter = OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
    with patch("aiohttp.ClientSession", return_value=_session(get=[_resp(body, 500)] * 5)), \
            patch("src.adapters.microsoft.onedrive_adapter.asyncio.sleep", new=AsyncMock()), \
            pytest.raises(ValueError):
        await adapter.get_change(drive_handle(user_id="u1"), "X!1")
    assert MARKER not in caplog.text and "failed (500): generalException/inner" in caplog.text
