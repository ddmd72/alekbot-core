"""
OAuthCredential.drive_id (USER_DRIVE_INDEXING_RFC §10.1 step 0): "" by default and carried through every Graph token
refresh. Its storage is asserted by the credential store kit (`test_credential_store_kit.py`: only a OneDrive record
carries it, a refresh keeps it); the old adapter's round-trip and legacy-document tests went with that adapter
(SECRETS_AT_REST_RFC §2.7).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from src.domain.integration import Integration
from src.domain.oauth_credential import OAuthCredential
from src.domain.provider_account import ProviderAccount
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from tests.support.oauth_credentials_store import mirror_get


def _creds(**kw) -> OAuthCredential:
    base = dict(user_id="u1", account_id="acct-1", kind="microsoft_onedrive", integration=Integration.DRIVE,
                account=ProviderAccount(realm="microsoft", subject="", label=""), access_token="old",
                refresh_token="r1", token_expiry=datetime(2026, 10, 11, 12, tzinfo=timezone.utc), scopes=[])
    return OAuthCredential(**{**base, **kw})


def test_default_is_empty():
    assert _creds().drive_id == ""


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post_resp):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.return_value = post_resp
    return s


class TestRefresh:
    async def test_refresh_carries_drive_id_and_clears_the_flag(self):
        oauth = AsyncMock(spec=OAuthCredentialsPort)
        oauth.get.return_value = _creds(
            drive_id="D1", needs_reconnect=True,
            token_expiry=datetime.now(timezone.utc) + timedelta(minutes=1))
        mirror_get(oauth)
        oauth.update.return_value = True                          # the record still exists
        provider = MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_onedrive",
                                               "Files.ReadWrite.AppFolder offline_access")
        session = _session(_resp({"access_token": "new", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.user_headers("u1") == {"Authorization": "Bearer new"}
        oauth.save.assert_not_called()                            # a refresh never creates a record
        saved = oauth.update.call_args.args[0]
        assert saved.drive_id == "D1"
        assert saved.needs_reconnect is False      # a successful refresh is the proof
