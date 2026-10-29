"""The drive connect callback saves the connected drive's id (USER_DRIVE_INDEXING_RFC §10.1 step 0)."""
from unittest.mock import DEFAULT, AsyncMock, MagicMock, Mock, patch

import pytest
from quart import Quart

from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.web.oauth_app import create_oauth_blueprint

_APPROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"
_ME = "https://graph.microsoft.com/v1.0/me"


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post, get):
    """`get` answers the drive-id read (the App Folder item). The account identity read (Graph /me, required since
    SECRETS_AT_REST_RFC I9) answers with a valid synthetic account: these tests are about the drive id, and the failed
    identity read is `test_consent_identity.py`'s."""
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.return_value = post
    me = _resp({"id": "ms-subject-1", "mail": "user@example.com"})

    def answer(url, **kwargs):
        if url == _ME:
            return me
        if isinstance(get, Exception):
            raise get
        return DEFAULT

    s.get.side_effect = answer
    if not isinstance(get, Exception):
        s.get.return_value = get
    return s


@pytest.fixture
def oauth_port():
    return AsyncMock(spec=OAuthCredentialsPort)


def _app(oauth_port):
    session_service = Mock()
    session_service.verify_access_token = Mock(return_value={"sub": "u1"})
    auth_config = Mock()
    auth_config.oauth_redirect_uri = "https://example.test/auth/callback"
    app = Quart("test_drive_oauth_drive_id")
    app.register_blueprint(create_oauth_blueprint(
        auth_service=Mock(), session_service=session_service, auth_registry=Mock(),
        auth_config=auth_config, oauth_credentials_port=oauth_port,
        ms_todo_client_id="cid", ms_todo_client_secret="csecret",
        onedrive_redirect_uri="https://example.test/auth/connect-onedrive/callback",
    ))
    return app


_TOKEN = {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
          "scope": "Files.ReadWrite.AppFolder offline_access"}


async def _callback(oauth_port, session):
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", "u1")
        with patch("aiohttp.ClientSession", return_value=session):
            return await client.get("/auth/connect-onedrive/callback?code=c&state=s1")


async def test_drive_id_read_from_the_app_folder_item_and_saved(oauth_port):
    approot = _resp({"id": "R!1", "parentReference": {"driveId": "d1", "driveType": "personal"}})
    session = _session(_resp(_TOKEN), approot)
    resp = await _callback(oauth_port, session)
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    assert session.get.call_args.args[0] == _APPROOT
    assert session.get.call_args.kwargs["headers"] == {"Authorization": "Bearer a"}
    assert oauth_port.save_credentials.call_args.args[0].drive_id == "d1"


async def test_approot_refused_still_connects_without_drive_id(oauth_port):
    resp = await _callback(oauth_port, _session(_resp(_TOKEN), _resp({}, status=401)))
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    assert oauth_port.save_credentials.call_args.args[0].drive_id == ""


async def test_approot_network_error_still_connects(oauth_port):
    resp = await _callback(oauth_port, _session(_resp(_TOKEN), OSError("boom")))
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    assert oauth_port.save_credentials.call_args.args[0].drive_id == ""


async def test_reconnect_saves_a_cleared_flag(oauth_port):
    approot = _resp({"id": "R!1", "parentReference": {"driveId": "d1"}})
    await _callback(oauth_port, _session(_resp(_TOKEN), approot))
    assert oauth_port.save_credentials.call_args.args[0].needs_reconnect is False
