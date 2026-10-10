"""Wire tests for MicrosoftGraphTokenProvider. Mock boundary: aiohttp.ClientSession."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.graph_auth import GraphReauthRequired, MicrosoftGraphTokenProvider
from src.domain.email import OAuthCredentials
from src.ports.oauth_credentials_port import OAuthCredentialsPort


def _creds(delta: timedelta, token: str = "old") -> OAuthCredentials:
    return OAuthCredentials(user_id="u1", provider="microsoft_onedrive", access_token=token,
                            refresh_token="r1", token_expiry=datetime.now(timezone.utc) + delta,
                            scopes=[], email_address="")


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


def _provider(creds):
    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.get_credentials.return_value = creds
    return MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_onedrive",
                                       "Files.ReadWrite.AppFolder offline_access"), oauth


class TestTokenProvider:
    async def test_none_when_not_connected(self):
        provider, _ = _provider(None)
        assert await provider.headers("u1") is None

    async def test_valid_token_cached_one_store_read(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        assert await provider.headers("u1") == {"Authorization": "Bearer old"}
        await provider.headers("u1")
        assert oauth.get_credentials.await_count == 1
        oauth.save_credentials.assert_not_called()

    async def test_expiring_token_refreshed_with_own_scope_and_provider(self):
        provider, oauth = _provider(_creds(timedelta(minutes=1)))
        session = _session(_resp({"access_token": "new", "expires_in": 3600, "refresh_token": "r2"}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1") == {"Authorization": "Bearer new"}
        assert session.post.call_args.kwargs["data"]["scope"] == "Files.ReadWrite.AppFolder offline_access"
        saved = oauth.save_credentials.call_args.args[0]
        assert saved.provider == "microsoft_onedrive" and saved.refresh_token == "r2"

    async def test_cache_expires_after_ttl(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        token, expiry, _ = provider._cache["u1"]
        provider._cache["u1"] = (token, expiry, datetime.now(timezone.utc) - timedelta(minutes=6))
        await provider.headers("u1")
        assert oauth.get_credentials.await_count == 2  # a Cabinet disconnect is seen within 5 minutes

    async def test_force_refresh_ignores_cache(self):
        provider, _ = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        session = _session(_resp({"access_token": "fresh", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1", force_refresh=True) == {"Authorization": "Bearer fresh"}

    async def test_invalid_grant_raises_reauth(self):
        provider, _ = _provider(_creds(timedelta(minutes=1)))
        session = _session(_resp({"error": "invalid_grant"}, status=400))
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(GraphReauthRequired):
            await provider.headers("u1")


class TestCacheIsolationAndInvalidation:
    """Review additions (Task 3): the behaviours Tasks 4 and 9 rely on but the plan's tests left unpinned."""

    async def test_invalidate_rereads_store(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        provider.invalidate("u1")
        await provider.headers("u1")
        assert oauth.get_credentials.await_count == 2

    async def test_cached_token_inside_refresh_margin_is_refreshed(self):
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        token, _, fetched_at = provider._cache["u1"]
        provider._cache["u1"] = (token, datetime.now(timezone.utc) + timedelta(minutes=4), fetched_at)
        oauth.get_credentials.return_value = _creds(timedelta(minutes=4))
        session = _session(_resp({"access_token": "new", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            assert await provider.headers("u1") == {"Authorization": "Bearer new"}
        session.post.assert_called_once()

    async def test_providers_never_share_a_token(self):
        oauth = AsyncMock(spec=OAuthCredentialsPort)
        oauth.get_credentials.side_effect = lambda user_id, provider: _creds(timedelta(hours=1), token=provider)
        todo = MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_todo", "Tasks.ReadWrite offline_access")
        drive = MicrosoftGraphTokenProvider(oauth, "cid", "csecret", "microsoft_onedrive",
                                            "Files.ReadWrite.AppFolder offline_access")
        assert await todo.headers("u1") == {"Authorization": "Bearer microsoft_todo"}
        assert await drive.headers("u1") == {"Authorization": "Bearer microsoft_onedrive"}
        assert [c.args[1] for c in oauth.get_credentials.await_args_list] == ["microsoft_todo", "microsoft_onedrive"]

    async def test_other_refresh_error_is_value_error_not_reauth(self):
        provider, oauth = _provider(_creds(timedelta(minutes=1)))
        session = _session(_resp({"error": "server_error"}, status=503))
        with patch("aiohttp.ClientSession", return_value=session), pytest.raises(ValueError) as exc_info:
            await provider.headers("u1")
        assert not isinstance(exc_info.value, GraphReauthRequired)
        oauth.save_credentials.assert_not_called()
        assert "u1" not in provider._cache


def _slow_session(post_resp):
    """A session whose token exchange yields to the event loop once, so callers really overlap."""
    s = _session(post_resp)

    async def enter():
        await asyncio.sleep(0)
        return post_resp
    post_resp.__aenter__ = AsyncMock(side_effect=enter)
    return s


class TestConcurrency:
    """One token exchange per user, however many coroutines ask at once (per-user asyncio.Lock)."""

    async def test_concurrent_callers_refresh_once(self):
        provider, oauth = _provider(_creds(timedelta(minutes=1)))
        session = _slow_session(_resp({"access_token": "new", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            results = await asyncio.gather(*(provider.headers("u1") for _ in range(5)))
        assert results == [{"Authorization": "Bearer new"}] * 5
        assert session.post.call_count == 1
        assert oauth.get_credentials.await_count == 1

    async def test_concurrent_forced_refresh_is_one_exchange(self):
        """A burst of 401s (every in-flight call invalidates + forces a refresh) costs one refresh."""
        provider, oauth = _provider(_creds(timedelta(hours=1)))
        await provider.headers("u1")
        session = _slow_session(_resp({"access_token": "fresh", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            results = await asyncio.gather(*(provider.headers("u1", force_refresh=True) for _ in range(3)))
        assert results == [{"Authorization": "Bearer fresh"}] * 3
        assert session.post.call_count == 1

    async def test_forced_refresh_after_a_completed_one_still_refreshes(self):
        """A forced refresh asked *after* the previous one finished is honoured (the token may be dead)."""
        provider, _ = _provider(_creds(timedelta(hours=1)))
        session = _session(_resp({"access_token": "fresh", "expires_in": 3600}))
        with patch("aiohttp.ClientSession", return_value=session):
            await provider.headers("u1", force_refresh=True)
            await asyncio.sleep(0.001)
            await provider.headers("u1", force_refresh=True)
        assert session.post.call_count == 2
