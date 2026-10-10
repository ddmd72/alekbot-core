"""Drive connect routes (docs/10_rfcs/USER_DRIVE_RFC.md §4.1)."""
import inspect
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from quart import Quart

from src.adapters.microsoft import ONEDRIVE_PROVIDER
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.web import oauth_app
from src.web.oauth_app import create_oauth_blueprint


def _resp(json_data, status=200):
    r = MagicMock()
    r.status = status
    r.ok = status < 300
    r.json = AsyncMock(return_value=json_data)
    r.text = AsyncMock(return_value=str(json_data))
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _session(post):
    s = MagicMock()
    s.__aenter__ = AsyncMock(return_value=s)
    s.__aexit__ = AsyncMock(return_value=False)
    s.post.return_value = post
    return s


@pytest.fixture
def oauth_port():
    return AsyncMock(spec=OAuthCredentialsPort)


def _app(oauth_port, redirect="https://example.test/auth/connect-onedrive/callback"):
    session_service = Mock()
    session_service.verify_access_token = Mock(return_value={"sub": "u1"})
    auth_config = Mock()
    auth_config.oauth_redirect_uri = "https://example.test/auth/callback"
    app = Quart("test_drive_oauth")
    app.register_blueprint(create_oauth_blueprint(
        auth_service=Mock(), session_service=session_service, auth_registry=Mock(),
        auth_config=auth_config, oauth_credentials_port=oauth_port,
        ms_todo_client_id="cid", ms_todo_client_secret="csecret", onedrive_redirect_uri=redirect,
    ))
    return app


async def test_connect_redirects_with_app_folder_scope(oauth_port):
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "access_token", "valid")
        resp = await client.get("/auth/connect-onedrive")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "login.microsoftonline.com/consumers/oauth2/v2.0/authorize" in location
    assert "Files.ReadWrite.AppFolder" in location and "offline_access" in location


async def test_callback_saves_credentials_under_drive_provider(oauth_port):
    token = _resp({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                   "scope": "Files.ReadWrite.AppFolder offline_access"})
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", "u1")
        with patch("aiohttp.ClientSession", return_value=_session(token)):
            resp = await client.get("/auth/connect-onedrive/callback?code=c&state=s1")
    assert resp.headers["Location"] == "/cabinet?drive_connected=1"
    saved = oauth_port.save_credentials.call_args.args[0]
    assert saved.provider == ONEDRIVE_PROVIDER and saved.user_id == "u1" and saved.refresh_token == "r"


async def test_callback_state_mismatch(oauth_port):
    async with _app(oauth_port).test_client() as client:
        client.set_cookie("localhost", "drive_oauth_state", "s1")
        client.set_cookie("localhost", "drive_connect_user_id", "u1")
        resp = await client.get("/auth/connect-onedrive/callback?code=c&state=evil")
    assert resp.headers["Location"] == "/cabinet?drive_error=state"
    oauth_port.save_credentials.assert_not_called()


async def test_501_when_not_configured(oauth_port):
    async with _app(oauth_port, redirect="").test_client() as client:
        client.set_cookie("localhost", "access_token", "valid")
        resp = await client.get("/auth/connect-onedrive")
    assert resp.status_code == 501


def test_provider_key_matches_adapter():
    assert f'"{ONEDRIVE_PROVIDER}"' in inspect.getsource(oauth_app)


def test_state_compared_constant_time():
    # Review (Task 9): the query `state` is attacker-chosen, the cookie is the secret.
    src = inspect.getsource(oauth_app)
    drive_callback = src[src.index("def connect_onedrive_callback"):]
    assert "secrets.compare_digest(stored_state, state)" in drive_callback
