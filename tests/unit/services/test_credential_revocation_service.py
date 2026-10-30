"""CredentialRevocationService: the provider-side revoke rule (MULTI_DRIVE_RFC §4.3) over the real credential store."""
import logging

import pytest

from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope
from src.ports.oauth_provider_port import OAuthProviderPort
from src.services.credential_revocation_service import CredentialRevocationService
from tests.support.oauth_credentials_store import CredentialsStore, creds

SHARED = ProviderAccount(realm="google", subject="sub-1", label="user@example.com")


class FakeEdge(OAuthProviderPort):
    def __init__(self, realm, scope, fail=False):
        self._realm, self._scope, self.fail, self.revoked = realm, scope, fail, []

    realm = property(lambda self: self._realm)
    revocation_scope = property(lambda self: self._scope)

    def authorization_url(self, flow, spec, redirect_uri, *, state):
        raise NotImplementedError

    async def exchange(self, code, flow, spec, redirect_uri):
        raise NotImplementedError

    async def revoke(self, credential):
        if self.fail:
            raise RuntimeError("provider down")
        self.revoked.append(credential.credential_id)


def _world(fail=False):
    store = CredentialsStore()
    google = FakeEdge("google", RevocationScope.GRANT_WIDE, fail)
    microsoft = FakeEdge("microsoft", RevocationScope.NONE)
    service = CredentialRevocationService(store.repo, {"google": google, "microsoft": microsoft})
    return store, service, google, microsoft


async def _id(store, user, kind):
    return (await store.repo.list_for_user(user, kind))[0].credential_id


async def test_gmail_removed_while_tasks_of_same_account_remains_does_not_revoke():
    store, service, google, _ = _world()
    await store.repo.save(creds("user-alex", "gmail", account=SHARED))
    await store.repo.save(creds("user-alex", "google_tasks", account=SHARED))
    assert await service.remove("user-alex", await _id(store, "user-alex", "gmail")) is True
    assert google.revoked == []
    assert len(await store.repo.list_for_user("user-alex")) == 1


async def test_last_credential_of_account_revokes_once_across_users():
    store, service, google, _ = _world()
    await store.repo.save(creds("user-1", "gmail", account=SHARED))
    await store.repo.save(creds("user-2", "gmail", account=SHARED))
    first, second = await _id(store, "user-1", "gmail"), await _id(store, "user-2", "gmail")
    await service.remove("user-1", first)
    assert google.revoked == []
    await service.remove("user-2", second)
    assert google.revoked == [second]


async def test_microsoft_never_revokes():
    store, service, _, microsoft = _world()
    await store.repo.save(creds("user-alex", "microsoft_todo"))
    assert await service.remove("user-alex", await _id(store, "user-alex", "microsoft_todo")) is True
    assert microsoft.revoked == []
    assert await store.repo.list_for_user("user-alex") == []


async def test_revoke_failure_is_logged_and_removal_still_true(caplog):
    store, service, google, _ = _world(fail=True)
    await store.repo.save(creds("user-alex", "gmail", account=SHARED))
    cid = await _id(store, "user-alex", "gmail")
    with caplog.at_level(logging.WARNING):
        assert await service.remove("user-alex", cid) is True
    assert await store.repo.list_for_user("user-alex") == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("revoke failed" in m for m in warnings)
    assert not any("at-old" in m or "alex@example.com" in m or cid in m for m in warnings)


async def test_unknown_credential_returns_false_and_calls_nothing():
    store, service, google, microsoft = _world()
    assert await service.remove("user-alex", "cred-1") is False
    assert google.revoked == [] and microsoft.revoked == []
