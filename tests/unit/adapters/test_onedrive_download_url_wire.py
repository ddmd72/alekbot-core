"""Wire tests: `OneDriveAdapter.download_url` (USER_DRIVE_INDEXING_RFC §27.4).

The URL is the pre-authenticated, short-lived `@microsoft.graph.downloadUrl` of the item, handed to ffmpeg to
stream from. It is a secret-bearing link: never logged, never in an exception message, and never fetched here
(so the bearer token cannot reach its host). Mock boundary: aiohttp.ClientSession."""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveItemNotFoundError
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get

_URL = "https://download.example.invalid/secret-token-abc123/clip.mp4"
_FILE = {"id": "f1", "name": "clip.mp4", "size": 10, "file": {"mimeType": "video/mp4"},
         "parentReference": {"path": "/drive/root:/Apps/Alek-bot"},
         "@microsoft.graph.downloadUrl": _URL}


def _adapter():
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get.return_value = OAuthCredential(
        user_id="u1",
        account_id="acct-1",
        kind=ONEDRIVE_PROVIDER,
        integration=Integration.DRIVE,
        account=ProviderAccount(realm="microsoft", subject="", label=""),
        access_token="tok",
        refresh_token="r",
        token_expiry=datetime.now(timezone.utc) + timedelta(hours=1),
        scopes=[],
    )
    mirror_get(oauth)
    return OneDriveAdapter(oauth, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))


def _resp(json_data=None, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    return s


async def test_returns_download_url():
    session = _session([_resp(_FILE)])
    with patch("aiohttp.ClientSession", return_value=session):
        assert await _adapter().download_url("u1", "f1") == _URL


async def test_missing_url_raises_not_found():
    session = _session([_resp({k: v for k, v in _FILE.items() if k != "@microsoft.graph.downloadUrl"})])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await _adapter().download_url("u1", "f1")


async def test_404_raises_not_found():
    session = _session([_resp({"error": {"code": "itemNotFound"}}, status=404)])
    with patch("aiohttp.ClientSession", return_value=session), pytest.raises(DriveItemNotFoundError):
        await _adapter().download_url("u1", "f1")


async def test_url_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    adapter = _adapter()
    with patch("aiohttp.ClientSession", return_value=_session([_resp(_FILE)])):
        await adapter.download_url("u1", "f1")
    missing = {k: v for k, v in _FILE.items() if k != "@microsoft.graph.downloadUrl"}
    with patch("aiohttp.ClientSession", return_value=_session([_resp(missing)])), \
            pytest.raises(DriveItemNotFoundError) as exc:
        await adapter.download_url("u1", "f1")
    assert "secret-token-abc123" not in caplog.text
    assert "download.example.invalid" not in caplog.text
    assert "secret-token-abc123" not in str(exc.value)


async def test_no_bearer_sent_to_the_url_host():
    """Only the Graph item read happens; the pre-authenticated URL is returned, not fetched."""
    session = _session([_resp(_FILE)])
    with patch("aiohttp.ClientSession", return_value=session):
        await _adapter().download_url("u1", "f1")
    assert session.get.call_count == 1
    assert session.get.call_args.args[0].startswith("https://graph.microsoft.com/")
    assert all(c.args[0] != _URL for c in session.get.call_args_list)
