"""MicrosoftGraphTokenProvider keyed by (user_id, credential_id) (MULTI_DRIVE_RFC §5.1, E3): two credentials of one
user never share a token, a cache entry or a refresh; the single-credential lookup (`user_headers`, until slice 1b
passes a handle) follows a reconnect of another account. The real credential store over the evaluating Firestore fake;
the token endpoint is the aiohttp boundary."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.microsoft.graph_auth import MicrosoftGraphTokenProvider
from tests.support.oauth_credentials_store import CredentialsStore, creds

USER = "user-alex"
KIND = "microsoft_onedrive"
LATE = datetime.now(timezone.utc) + timedelta(hours=2)
SOON = datetime.now(timezone.utc) + timedelta(minutes=1)


def _token_session(payload: dict):
    resp = MagicMock(status=200)
    resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post.return_value = resp
    return session


@pytest.fixture
def store() -> CredentialsStore:
    return CredentialsStore()


async def _two(store: CredentialsStore, *, expiry_a=LATE, expiry_b=LATE):
    """Two drive credentials of one user, two accounts."""
    await store.repo.save(creds(USER, KIND, access_token="at-a", token_expiry=expiry_a,
                                account=replace(creds(USER, KIND).account, subject="sub-1")))
    await store.repo.save(creds(USER, KIND, access_token="at-b", token_expiry=expiry_b,
                                account=replace(creds(USER, KIND).account, subject="sub-2")))
    by_subject = {c.account.subject: c for c in await store.repo.list_for_user(USER, KIND)}
    return by_subject["sub-1"], by_subject["sub-2"]


def _provider(store: CredentialsStore) -> MicrosoftGraphTokenProvider:
    return MicrosoftGraphTokenProvider(store.repo, "cid", "csecret", KIND, "Files.ReadWrite.AppFolder offline_access")


async def test_two_credentials_of_one_user_get_their_own_tokens(store):
    a, b = await _two(store)
    tokens = _provider(store)
    assert await tokens.headers(USER, a.credential_id) == {"Authorization": "Bearer at-a"}
    assert await tokens.headers(USER, b.credential_id) == {"Authorization": "Bearer at-b"}
    assert tokens.cached_token(USER, a.credential_id) == "at-a"
    assert tokens.cached_token(USER, b.credential_id) == "at-b"


async def test_invalidating_one_credential_keeps_the_other_cached(store):
    a, b = await _two(store)
    tokens = _provider(store)
    await tokens.headers(USER, a.credential_id)
    await tokens.headers(USER, b.credential_id)
    tokens.invalidate(USER, a.credential_id)
    assert tokens.cached_token(USER, a.credential_id) is None
    assert tokens.cached_token(USER, b.credential_id) == "at-b"


async def test_a_refresh_of_one_credential_writes_only_that_record(store):
    a, b = await _two(store, expiry_a=SOON)
    tokens = _provider(store)
    with patch("aiohttp.ClientSession", return_value=_token_session({"access_token": "at-a2", "expires_in": 3600})):
        assert await tokens.headers(USER, a.credential_id) == {"Authorization": "Bearer at-a2"}
    got_a, got_b = await store.repo.get(USER, a.credential_id), await store.repo.get(USER, b.credential_id)
    assert (got_a.access_token, got_b.access_token) == ("at-a2", "at-b")
    assert replace(got_a, access_token="at-a", token_expiry=a.token_expiry) == a      # identity carried
    assert got_b == b


async def test_a_credential_that_is_gone_is_not_connected(store):
    a, b = await _two(store)
    tokens = _provider(store)
    await store.repo.delete(USER, a.credential_id)
    assert await tokens.headers(USER, a.credential_id) is None
    assert await tokens.headers(USER, b.credential_id) == {"Authorization": "Bearer at-b"}


async def test_user_headers_follows_a_reconnect_of_another_account(store):
    """The held credential id is gone (another account replaced the record): looked up again at once, never 'not
    connected' for the cache's lifetime."""
    await store.repo.save(creds(USER, KIND, access_token="at-old", token_expiry=LATE))
    tokens = _provider(store)
    assert await tokens.user_headers(USER) == {"Authorization": "Bearer at-old"}
    (old,) = await store.repo.list_for_user(USER, KIND)
    await store.repo.delete(USER, old.credential_id)
    await store.repo.save(creds(USER, KIND, access_token="at-new", token_expiry=LATE,
                                account=replace(old.account, subject="sub-2")))
    tokens.invalidate(USER, old.credential_id)          # the old token's cache entry expired
    assert await tokens.user_headers(USER) == {"Authorization": "Bearer at-new"}
    (new,) = await store.repo.list_for_user(USER, KIND)
    assert tokens.held_credential_id(USER) == new.credential_id


async def test_user_headers_of_a_user_without_the_kind_is_none(store):
    await store.repo.save(creds(USER, "microsoft_todo", token_expiry=LATE))
    tokens = _provider(store)
    assert await tokens.user_headers(USER) is None
    assert tokens.held_credential_id(USER) is None


async def test_forget_drops_the_held_id_and_every_token_of_the_user(store):
    a, b = await _two(store)
    await store.repo.save(creds("user-clara", KIND, access_token="at-c", token_expiry=LATE))
    (c,) = await store.repo.list_for_user("user-clara", KIND)
    tokens = _provider(store)
    await tokens.user_headers(USER)
    await tokens.headers(USER, b.credential_id)
    await tokens.headers("user-clara", c.credential_id)
    tokens.forget(USER)
    assert tokens.held_credential_id(USER) is None
    assert tokens.cached_token(USER, a.credential_id) is None and tokens.cached_token(USER, b.credential_id) is None
    assert tokens.cached_token("user-clara", c.credential_id) == "at-c"
