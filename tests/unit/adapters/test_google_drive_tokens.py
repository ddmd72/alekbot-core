"""GoogleDriveTokenProvider (GOOGLE_DRIVE_RFC §3.1, §6 row "Token provider", GE-16): keyed by (user_id, credential_id),
concurrent refreshes deduped under the per-key lock, the refreshed token written back through the credential port,
`invalid_grant` → DriveReauthRequired, any other token-endpoint failure → DriveThrottled, `forget` drops the key.

The token endpoint is the stateful Drive fake's (`tests/support/google_drive_fake.py`), the credentials the real
store's."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.google.google_drive_tokens import CACHE_TTL, TOKEN_URL, GoogleDriveTokenProvider
from src.domain.user_drive import DriveReauthRequired, DriveThrottled
from tests.support.google_drive_fake import google_setup

_PAST = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _provider(setup):
    return GoogleDriveTokenProvider(setup.store.repo, "cid", "csecret", "google_drive")


@pytest.fixture
async def setup(monkeypatch):
    return await google_setup(monkeypatch)


@pytest.fixture
async def expired(monkeypatch):
    return await google_setup(monkeypatch, token_expiry=_PAST)


async def test_tokens_are_keyed_by_user_and_credential(setup):
    provider = _provider(setup)
    a, b = setup.handle, setup.other
    token_a, _ = await provider.token(a.user_id, a.credential_id)
    token_b, _ = await provider.token(b.user_id, b.credential_id)
    assert (token_a, token_b) == ("tok-a", "tok-b")
    assert provider.holds(a.user_id, a.credential_id) and provider.holds(b.user_id, b.credential_id)
    assert not provider.holds(a.user_id, "cred-unknown")
    assert await provider.token(a.user_id, "cred-unknown") is None          # not stored: not connected
    assert setup.fake.calls("token") == []                                  # both live an hour: no refresh


async def test_a_cached_token_is_served_without_a_store_read(setup):
    provider = _provider(setup)
    h = setup.handle
    await provider.token(h.user_id, h.credential_id)
    reads = []
    real_get = setup.store.repo.get

    async def counting_get(*args):
        reads.append(args)
        return await real_get(*args)

    setup.store.repo.get = counting_get
    assert (await provider.token(h.user_id, h.credential_id))[0] == "tok-a"
    assert reads == []
    assert CACHE_TTL == timedelta(minutes=5)


async def test_an_expiring_token_is_refreshed_and_written_back(expired):
    provider = _provider(expired)
    h = expired.handle
    token, expiry = await provider.token(h.user_id, h.credential_id)
    assert token.startswith("tok-perm-a-") and expiry > datetime.now(timezone.utc) + timedelta(minutes=50)
    call = expired.fake.calls("token")
    assert len(call) == 1 and call[0].url == TOKEN_URL
    stored = await expired.store.repo.get(h.user_id, h.credential_id)
    assert stored.access_token == token and stored.refresh_token == "rt-a" and stored.token_expiry == expiry
    assert stored.needs_reconnect is False


async def test_concurrent_refreshes_of_one_grant_are_one_token_call(expired):
    provider = _provider(expired)
    h = expired.handle
    results = await asyncio.gather(*(provider.token(h.user_id, h.credential_id) for _ in range(5)))
    assert len(expired.fake.calls("token")) == 1
    assert len({token for token, _ in results}) == 1


async def test_concurrent_forced_refreshes_are_one_token_call(setup):
    provider = _provider(setup)
    h = setup.handle
    await provider.token(h.user_id, h.credential_id)
    results = await asyncio.gather(*(provider.token(h.user_id, h.credential_id, force_refresh=True)
                                     for _ in range(3)))
    assert len(setup.fake.calls("token")) == 1
    assert {token for token, _ in results} != {"tok-a"} and len({token for token, _ in results}) == 1


async def test_invalid_grant_is_reauth_required(expired, caplog):
    expired.fake.refresh_tokens.clear()                     # Google no longer knows the refresh token
    provider = _provider(expired)
    h = expired.handle
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DriveReauthRequired):
            await provider.token(h.user_id, h.credential_id)
    assert "invalid_grant" in caplog.text
    assert "rt-a" not in caplog.text and "revoked" not in caplog.text    # never a token nor the body's free text


@pytest.mark.parametrize("failure", [500, 503, "timeout", "transport"])
async def test_a_failing_token_endpoint_is_throttled(expired, failure):
    expired.fake.fail("token", failure)
    provider = _provider(expired)
    h = expired.handle
    with pytest.raises(DriveThrottled):
        await provider.token(h.user_id, h.credential_id)
    stored = await expired.store.repo.get(h.user_id, h.credential_id)
    assert stored.access_token == "tok-a"                    # nothing written


async def test_a_grant_deleted_during_the_refresh_is_not_connected(expired):
    h = expired.handle
    expired.store.disconnect_after_read(h.user_id, "google_drive")
    provider = _provider(expired)
    assert await provider.token(h.user_id, h.credential_id) is None
    assert await expired.store.repo.get(h.user_id, h.credential_id) is None   # not re-created


async def test_forget_drops_the_key(setup):
    h, other = setup.handle, setup.other
    await setup.adapter.get_root(h)
    await setup.adapter.get_root(other)
    tokens = setup.adapter._tokens
    assert tokens.holds(h.user_id, h.credential_id)
    await setup.adapter.forget(h)
    assert not tokens.holds(h.user_id, h.credential_id) and tokens.holds(other.user_id, other.credential_id)


async def test_a_token_living_less_than_asked_is_refreshed_first(monkeypatch):
    setup = await google_setup(monkeypatch, token_expiry=datetime.now(timezone.utc) + timedelta(minutes=7))
    provider = _provider(setup)
    h = setup.handle
    assert (await provider.token(h.user_id, h.credential_id))[0] == "tok-a"        # 7 min > the 5 min margin
    token, expiry = await provider.token(h.user_id, h.credential_id, min_life=timedelta(minutes=10))
    assert token != "tok-a" and expiry > datetime.now(timezone.utc) + timedelta(minutes=10)
    assert len(setup.fake.calls("token")) == 1
