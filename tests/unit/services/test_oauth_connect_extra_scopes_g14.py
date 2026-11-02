"""G14 (GOOGLE_DRIVE_RFC §5, design §1.7, Δ5, Δ10): a grant carrying scopes beyond the asked ones and the edge's
identity scopes logs one WARNING with the kind and the count (never a scope string) and is stored as today; today's
grants of every kind log none. Over the real `OAuthConnectService` and the real edges' `identity_scopes`, with the
exchange scripted."""
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from src.adapters.google.google_oauth_provider import GoogleOAuthProvider
from src.adapters.microsoft.microsoft_oauth_provider import MicrosoftOAuthProvider
from src.composition.connector_registry import (
    GMAIL, GOOGLE_DRIVE, GOOGLE_TASKS, MICROSOFT_ONEDRIVE, MICROSOFT_TODO, ConnectorRegistry,
)
from src.domain.oauth_grant import GrantResult
from src.domain.provider_account import ProviderAccount
from src.ports.ephemeral_store import EphemeralStore
from src.services.oauth_connect_service import OAuthConnectService
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.oauth_credentials_store import CredentialsStore

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
REDIRECT = "https://service.example/auth/connect/callback"
USERINFO_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
# today's grants as each edge returns them (Google's long `userinfo.email`; Microsoft's short forms)
TODAY = {
    GMAIL: (GMAIL_SCOPE, "openid", USERINFO_EMAIL),
    GOOGLE_TASKS: ("https://www.googleapis.com/auth/tasks", "openid", USERINFO_EMAIL),
    GOOGLE_DRIVE: (DRIVE_SCOPE, "openid", USERINFO_EMAIL),
    MICROSOFT_TODO: ("Tasks.ReadWrite", "offline_access", "User.Read", "openid", "profile", "email"),
    MICROSOFT_ONEDRIVE: ("Files.ReadWrite.AppFolder", "offline_access", "User.Read"),
}


class _Flows(EphemeralStore):
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


def _scripted(edge_class):
    class Scripted(edge_class):
        """The real edge (its `identity_scopes`), its exchange scripted."""
        scopes: tuple = ()

        async def exchange(self, code, flow, spec, redirect_uri):
            return GrantResult(access_token="tok-a1", refresh_token="tok-b2", token_expiry=T0 + timedelta(hours=1),
                               scopes=self.scopes,
                               account=ProviderAccount(realm=self.realm, subject="sub-1", label="alex@example.com"))
    return Scripted("cid", "csecret")


class _World:
    def __init__(self):
        self.store = CredentialsStore()
        self.edges = {"google": _scripted(GoogleOAuthProvider), "microsoft": _scripted(MicrosoftOAuthProvider)}

        async def remove(user_id, credential_id):
            return True

        self.service = OAuthConnectService(self.store.repo, ConnectorRegistry().specs(), self.edges, _Flows(),
                                           AesGcmTestCipher(), b"k" * 32, remove, {}, REDIRECT, clock=lambda: T0)

    async def connect(self, kind, scopes):
        spec = ConnectorRegistry().get(kind)
        self.edges[spec.realm].scopes = tuple(scopes)
        url = await self.service.start("usr-1", kind)
        state = parse_qs(urlparse(url).query)["state"][0]
        return await self.service.complete(state, "code-1", session_user_id="usr-1", session_account_id="acc-1")


def _g14(caplog):
    return [r for r in caplog.records if "beyond the asked ones" in r.getMessage()]


async def test_a_drive_grant_carrying_gmails_scope_warns_once_with_the_count_and_is_stored(caplog):
    caplog.set_level(logging.DEBUG)
    world = _World()
    out = await world.connect(GOOGLE_DRIVE, (DRIVE_SCOPE, GMAIL_SCOPE, "openid", USERINFO_EMAIL))
    assert out.status == "connected"
    (warning,) = _g14(caplog)
    message = warning.getMessage()
    assert warning.levelno == logging.WARNING
    assert "kind=google_drive" in message and "carries 1 scopes" in message
    assert "googleapis" not in message and "gmail" not in message
    (held,) = await world.store.repo.list_for_user("usr-1", GOOGLE_DRIVE)
    assert GMAIL_SCOPE in held.scopes


@pytest.mark.parametrize("kind", sorted(TODAY))
async def test_todays_grants_log_no_warning(kind, caplog):
    caplog.set_level(logging.DEBUG)
    out = await _World().connect(kind, TODAY[kind])
    assert out.status == "connected"
    assert _g14(caplog) == []
