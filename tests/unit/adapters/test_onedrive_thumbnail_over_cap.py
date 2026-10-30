"""Wire tests: a rendition over the vision cap is told apart from no rendition (USER_DRIVE_INDEXING_RFC §24.5).
Mock boundary: aiohttp.ClientSession."""
from unittest.mock import patch

import pytest

from src.domain.user_drive import DriveThumbnailTooLarge

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.adapters.microsoft.onedrive_adapter import ONEDRIVE_PROVIDER, OneDriveAdapter
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get


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


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, size):
        async def gen():
            for chunk in self._chunks:
                yield chunk
        return gen()


def _resp(json_data=None, status=200, chunks=()):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.headers = {}
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.content = _Content(list(chunks))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(get):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.get.side_effect = get
    return s


def _thumbs(size):
    return {"value": [{"id": "0", size: {"height": 117, "width": 176, "url": "https://thumbs.example/t1"}}]}


async def test_over_cap_rendition_raises_with_size_and_limit():
    big = [b"x" * (1024 * 1024)] * 6
    session = _session([_resp(_thumbs("c2048x2048")), _resp(chunks=big)])
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(DriveThumbnailTooLarge) as caught:
            await _adapter().get_thumbnail("u1", "f1", "c2048x2048")
    assert caught.value.size == "c2048x2048" and caught.value.limit_bytes > 0


async def test_no_rendition_is_still_none():
    session = _session([_resp(_thumbs("c2048x2048"), status=200), _resp(status=404)])
    with patch("aiohttp.ClientSession", return_value=session):
        assert await _adapter().get_thumbnail("u1", "f1", "c2048x2048") is None
