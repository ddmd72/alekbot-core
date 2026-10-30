"""
Wire tests for the Microsoft identity token endpoint as MicrosoftGraphTokenProvider sees it
(USER_DRIVE_RFC §4.2, USER_DRIVE_INDEXING_RFC §4, §7.7, §8.2).

A refresh the user can only fix by signing in again (`invalid_grant`, `interaction_required`,
`consent_required`) is GraphReauthRequired, so the drive asks for a reconnect instead of retrying a dead
grant every minute.

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveReauthRequired
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry

_INTERACTION = {"error": "interaction_required",
                "error_description": "AADSTS50076: Due to a configuration change made by your administrator, "
                                     "or because you moved to a new location, you must use multi-factor "
                                     "authentication to access the resource.",
                "error_codes": [50076], "suberror": "basic_action"}
_CONSENT = {"error": "consent_required",
            "error_description": "AADSTS65001: The user or administrator has not consented to use the application.",
            "error_codes": [65001], "suberror": "consent_required"}


def _creds() -> OAuthCredential:
    return OAuthCredential(
        user_id="u1",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="old",
        refresh_token="r1",
        token_expiry=datetime.now(timezone.utc) - timedelta(minutes=1),
        scopes=[],
    )


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=json.dumps(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.side_effect = post
    return s


def _oauth():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = _creds()
    oauth.list_for_user.side_effect = lambda *a, **k: [oauth.get.return_value] if oauth.get.return_value else []
    return oauth


def _provider(oauth):
    return MicrosoftGraphTokenProvider(oauth, "cid", "csecret", ONEDRIVE_PROVIDER,
                                       "Files.ReadWrite.AppFolder offline_access")


class TestSignInRequiredIsReauth:
    @pytest.mark.parametrize("body", [_INTERACTION, _CONSENT], ids=["interaction_required", "consent_required"])
    async def test_token_provider_raises_reauth(self, body):
        oauth = _oauth()
        with patch("aiohttp.ClientSession", return_value=_session([_resp(body, status=400)])), \
                pytest.raises(GraphReauthRequired):
            await _provider(oauth).user_headers("u1")
        oauth.save.assert_not_called()

    @pytest.mark.parametrize("body", [_INTERACTION, _CONSENT], ids=["interaction_required", "consent_required"])
    async def test_drive_asks_for_a_reconnect(self, body):
        adapter = OneDriveAdapter(_oauth(), "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
        with patch("aiohttp.ClientSession", return_value=_session([_resp(body, status=400)])), \
                pytest.raises(DriveReauthRequired):
            await adapter.get_changes("u1", None)

    async def test_a_server_error_is_still_not_reauth(self):
        oauth = _oauth()
        body = {"error": "temporarily_unavailable", "error_description": "AADSTS90033: transient"}
        with patch("aiohttp.ClientSession", return_value=_session([_resp(body, status=503)])), \
                pytest.raises(ValueError) as exc:
            await _provider(oauth).user_headers("u1")
        assert not isinstance(exc.value, GraphReauthRequired)

