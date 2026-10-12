"""
OAuthCredentials.drive_id (USER_DRIVE_INDEXING_RFC §10.1 step 0): persisted by the Firestore adapter,
"" for documents written before the field existed, and carried through every Graph token refresh.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.firestore_oauth_credentials_adapter import FirestoreOAuthCredentialsAdapter
from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from src.domain.email import OAuthCredentials
from src.ports.oauth_credentials_port import OAuthCredentialsPort


def _creds(**kw) -> OAuthCredentials:
    base = dict(user_id="u1", provider="microsoft_onedrive", access_token="old", refresh_token="r1",
                token_expiry=datetime(2026, 10, 11, 12, tzinfo=timezone.utc), scopes=[], email_address="")
    return OAuthCredentials(**{**base, **kw})


def test_default_is_empty():
    assert _creds().drive_id == ""


class TestFirestore:
    def test_round_trips(self):
        doc = FirestoreOAuthCredentialsAdapter._to_firestore(_creds(drive_id="D1"))
        assert doc["drive_id"] == "D1"
        assert FirestoreOAuthCredentialsAdapter._from_firestore(doc).drive_id == "D1"

    def test_legacy_document_reads_empty(self):
        doc = FirestoreOAuthCredentialsAdapter._to_firestore(_creds())
        del doc["drive_id"]
        assert FirestoreOAuthCredentialsAdapter._from_firestore(doc).drive_id == ""

    def test_null_reads_empty(self):
        doc = {**FirestoreOAuthCredentialsAdapter._to_firestore(_creds()), "drive_id": None}
        assert FirestoreOAuthCredentialsAdapter._from_firestore(doc).drive_id == ""


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
        oauth.get_credentials.return_value = _creds(
            drive_id="D1", needs_reconnect=True,
            token_expiry=datetime.now(timezone.utc) + timedelta(minutes=1))
        provider = MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_onedrive",
                                               "Files.ReadWrite.AppFolder offline_access")
        session = _session(_resp({"access_token": "new", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1") == {"Authorization": "Bearer new"}
        saved = oauth.save_credentials.call_args.args[0]
        assert saved.drive_id == "D1"
        assert saved.needs_reconnect is False      # a successful refresh is the proof
