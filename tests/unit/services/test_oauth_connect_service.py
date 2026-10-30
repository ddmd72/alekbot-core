"""OAuthConnectService: the server-side connect flow (MULTI_DRIVE_RFC §5.2, T-11) over the real credential store."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import parse_qs, urlparse

import pytest

from src.domain.connector_spec import ConnectorSpec
from src.domain.integration import Integration
from src.domain.oauth_grant import GrantResult
from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope
from src.ports.ephemeral_store import EphemeralStore
from src.ports.oauth_provider_port import OAuthProviderPort
from src.services.oauth_connect_service import OAuthConnectService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.oauth_credentials_store import CredentialsStore

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
REDIRECT = "https://example.test/oauth/callback"
GMAIL = ConnectorSpec(kind="gmail", integration=Integration.MAIL, realm="google",
                      required_scopes=("gmail.read",), multiple=False, id_space="", label="Gmail")
DRIVES = ConnectorSpec(kind="google_drive", integration=Integration.DRIVE, realm="google",
                       required_scopes=("drive.read",), multiple=True, id_space="gd", label="Drive")


class MemoryStore(EphemeralStore):
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


class FakeEdge(OAuthProviderPort):
    realm = "google"
    revocation_scope = RevocationScope.GRANT_WIDE

    def __init__(self):
        self.subject, self.scopes, self.raises, self.revoked = "sub-1", ("gmail.read", "drive.read"), False, []
        self.exchanges = 0

    def authorization_url(self, flow, spec, redirect_uri, *, state):
        return f"https://idp.example/auth?state={state}&kind={spec.kind}"

    async def exchange(self, code, flow, spec, redirect_uri):
        self.exchanges += 1
        if self.raises:
            raise RuntimeError("bad code")
        return GrantResult(access_token="at-new", refresh_token="rt-new", token_expiry=T0 + timedelta(hours=1),
                           scopes=self.scopes,
                           account=ProviderAccount(realm="google", subject=self.subject, label="alex@example.com"))

    async def revoke(self, credential):
        self.revoked.append(credential.credential_id)


class World:
    def __init__(self, activation=None):
        self.now = T0
        self.store = CredentialsStore()
        self.flows = MemoryStore()
        self.edge = FakeEdge()
        self.removed = []
        self.activated = []

        async def remove(user_id, credential_id):
            self.removed.append((user_id, credential_id))
            await self.store.repo.delete(user_id, credential_id)
            return True

        async def activate(credential):
            self.activated.append(credential)
            if activation:
                raise activation

        self.service = OAuthConnectService(
            self.store.repo, {"gmail": GMAIL, "google_drive": DRIVES}, {"google": self.edge}, self.flows,
            AesGcmTestCipher(), b"k" * 32, remove, {Integration.MAIL: activate}, REDIRECT, clock=lambda: self.now)

    async def start(self, kind="gmail", user="user-alex", **kw):
        url = await self.service.start(user, kind, **kw)
        return parse_qs(urlparse(url).query)["state"][0]

    async def complete(self, state, user="user-alex"):
        return await self.service.complete(state, "code-1", session_user_id=user, session_account_id="acct-1")

    async def held(self, kind="gmail", user="user-alex"):
        return await self.store.repo.list_for_user(user, kind)


async def test_flow_is_single_use():
    w = World()
    state = await w.start()
    assert (await w.complete(state)).status == "connected"
    again = await w.complete(state)
    assert again.status == "state" and again.kind is None


async def test_unknown_kind_and_foreign_reconnect_raise():
    w = World()
    with pytest.raises(ValueError):
        await w.service.start("user-alex", "nope")
    with pytest.raises(ValueError):
        await w.service.start("user-alex", "gmail", credential_id="cred-1")


async def test_expired_at_ten_minutes():
    w = World()
    state = await w.start()
    w.now += timedelta(minutes=10, seconds=1)
    assert (await w.complete(state)).status == "state"
    assert await w.held() == []


async def test_other_session_user_stores_nothing():
    w = World()
    state = await w.start()
    out = await w.complete(state, user="user-clara")
    assert out.status == "user"
    assert await w.held() == [] and await w.held(user="user-clara") == []
    assert w.edge.exchanges == 0


async def test_reconnect_with_other_subject_is_account_and_touches_nothing():
    w = World()
    await w.complete(await w.start())
    old = (await w.held())[0]
    w.edge.subject = "sub-2"
    out = await w.complete(await w.start(credential_id=old.credential_id))
    assert out.status == "account"
    assert [c.credential_id for c in await w.held()] == [old.credential_id]
    assert w.removed == [] and w.edge.revoked == []


async def test_stored_flow_holds_no_raw_state_or_plaintext_verifier():
    w = World()
    state = await w.start()
    (key, record), = w.flows.data.items()
    assert state not in key and key.startswith("oauth_flow:")
    assert state not in repr(record)
    assert "pkce_verifier" not in record
    assert "alex@example.com" not in repr(record)
    assert record["pkce_verifier_ct"]


async def test_scope_missing():
    w = World()
    w.edge.scopes = ("other",)
    assert (await w.complete(await w.start())).status == "scope"
    assert await w.held() == [] and w.activated == []


async def test_exchange_failure():
    w = World()
    w.edge.raises = True
    assert (await w.complete(await w.start())).status == "exchange"
    assert await w.held() == []


async def test_same_account_updates_in_place():
    w = World()
    await w.complete(await w.start())
    first = (await w.held())[0]
    w.edge.scopes = ("gmail.read", "extra")
    await w.store.repo.update(replace(first, needs_reconnect=True))
    assert (await w.complete(await w.start())).status == "connected"
    (second,) = await w.held()
    assert second.credential_id == first.credential_id
    assert second.access_token == "at-new" and second.needs_reconnect is False
    assert "extra" in second.scopes
    assert w.removed == []


async def test_reconnect_updates_same_record():
    w = World()
    await w.complete(await w.start())
    first = (await w.held())[0]
    out = await w.complete(await w.start(credential_id=first.credential_id, return_path="/cabinet/x"))
    assert out.status == "connected" and out.return_path == "/cabinet/x"
    assert [c.credential_id for c in await w.held()] == [first.credential_id]


async def test_single_kind_second_account_replaces_old_through_remove_credential():
    w = World()
    await w.complete(await w.start())
    old = (await w.held())[0]
    w.edge.subject = "sub-2"
    assert (await w.complete(await w.start())).status == "connected"
    (now,) = await w.held()
    assert now.account.subject == "sub-2" and now.credential_id != old.credential_id
    assert w.removed == [("user-alex", old.credential_id)]


async def test_multiple_kind_second_account_keeps_both():
    w = World()
    await w.complete(await w.start("google_drive"))
    w.edge.subject = "sub-2"
    await w.complete(await w.start("google_drive"))
    assert len(await w.held("google_drive")) == 2
    assert w.removed == []


async def test_saved_with_account_id_and_integration_and_activation_runs_once():
    w = World()
    await w.complete(await w.start())
    (saved,) = await w.held()
    assert saved.account_id == "acct-1" and saved.integration is Integration.MAIL
    assert [a.credential_id for a in w.activated] == [saved.credential_id]


async def test_failing_activation_still_connected():
    w = World(activation=RuntimeError("boom"))
    assert (await w.complete(await w.start())).status == "connected"
    assert len(await w.held()) == 1


async def test_logs_carry_no_secrets(caplog):
    w = World()
    with caplog.at_level(logging.DEBUG):
        state = await w.start()
        await w.complete(state)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert state not in text and "code-1" not in text and "at-new" not in text and "alex@example.com" not in text
