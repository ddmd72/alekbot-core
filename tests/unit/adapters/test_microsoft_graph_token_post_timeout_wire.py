"""
Wire tests: the Microsoft identity token POST has its own bound (USER_DRIVE_RFC §4.2,
USER_DRIVE_INDEXING_RFC §8.2).

Shorter than a Graph call's: a hanging token endpoint ends as a TimeoutError after 30 s instead of
aiohttp's 300 s default, and the drive keeps classifying it as a systemic throttle. Shared with To Do,
which sees the same TimeoutError as before, only sooner.

Mock boundary: aiohttp.ClientSession (docs/how_to/ADAPTER_WIRE_TESTING.md).
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveThrottled
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry


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


class TestTokenPostTimeout:
    async def test_token_post_carries_its_own_30s_bound(self):
        session = _session([_resp({"access_token": "new", "expires_in": 3600})])
        with patch("aiohttp.ClientSession", return_value=session):
            await _provider(_oauth()).user_headers("u1")
        timeout = session.post.call_args.kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout) and timeout.total == 30

    async def test_a_timed_out_token_post_is_a_timeout_error(self):
        oauth = _oauth()

        def hang(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch("aiohttp.ClientSession", return_value=_session(hang)), pytest.raises(asyncio.TimeoutError):
            await _provider(oauth).user_headers("u1")
        oauth.save.assert_not_called()

    async def test_drive_keeps_a_timed_out_refresh_a_systemic_throttle(self):
        def hang(*args, **kwargs):
            raise asyncio.TimeoutError()

        adapter = OneDriveAdapter(_oauth(), "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
        with patch("aiohttp.ClientSession", return_value=_session(hang)), pytest.raises(DriveThrottled) as exc:
            await adapter.get_changes("u1", None)
        assert exc.value.retry_after_s == 60.0
