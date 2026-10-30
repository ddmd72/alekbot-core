"""The Cabinet session's 24 h cap at the instants that matter (P16-D6; `src/services/session_service.py`, `src/web/oauth_app.py`).

Real session service, real blueprint, real user/account/whitelist repositories on the evaluating Firestore fake; only
Google (the `AuthPort` boundary) is scripted. Tokens travel as the browser sends them, in the `access_token` and
`refresh_token` cookies (the routes read the cookie first). "Now" is pinned in the session service and in PyJWT (which reads the clock
when it checks `exp`), and re-pinned at every step.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt.api_jwt
import pytest
from quart import Quart

import src.services.session_service as session_module
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.services.session_service import SessionService
from src.web.oauth_app import create_oauth_blueprint
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import SESSION_SECRET, Tripwire
from src.composition.connector_registry import ConnectorRegistry

ALLOWED = "owner@example.com"
USERS, ACCOUNTS, WHITELIST = "users", "accounts", "whitelist"
DAY = 86400
T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)


class _Google(AuthPort):
    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="g-at", id_token="g-id", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        now = datetime.now(timezone.utc)
        return TokenClaims(sub="sub-1", iss="https://accounts.google.com", aud="client", exp=now + timedelta(hours=1),
                           iat=now, email=ALLOWED, email_verified=True)

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub="sub-1", email=ALLOWED, email_verified=True, name="Owner")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


def _pin(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    pin_datetime(monkeypatch, session_module, instant)
    pin_datetime(monkeypatch, jwt.api_jwt, instant)


async def _world():
    db, touched = EvalDb(), []
    env = SimpleNamespace(domain_users_collection=USERS, domain_whitelist_collection=WHITELIST, is_production=False)
    accounts = FirestoreAccountRepository(db, ACCOUNTS)
    users = FirestoreUserRepository(db, env, accounts)
    registry = AuthProviderRegistry({"google": _Google()}, default_provider_name="google")
    auth = AuthenticationService(registry, users, accounts, whitelist_repo=FirestoreWhitelistRepository(db, env))
    await db.collection(WHITELIST).document("config").set({"allowed_emails": [ALLOWED], "allowed_domains": []})
    await accounts.create_account(BillingAccount(account_id="a1"))
    await users.create_user(UserProfile(user_id="u1", account_id="a1", external_user_id="google|sub-1"))
    app = Quart("session_cap")
    app.register_blueprint(create_oauth_blueprint(
        auth_service=auth,
        session_service=SessionService(secret_key=SESSION_SECRET, access_token_ttl=DAY, refresh_token_ttl=DAY),
        auth_registry=registry,
        auth_config=SimpleNamespace(
            oauth_redirect_uri="https://app.example/auth/callback",
            gmail_oauth_redirect_uri="https://app.example/auth/connect-gmail/callback",
            google_tasks_oauth_redirect_uri="https://app.example/auth/connect-google-tasks/callback",
            access_token_ttl=DAY, refresh_token_ttl=DAY,
        ),
        gmail_oauth_service=Tripwire("gmail_oauth_service", touched),
        oauth_credentials_port=Tripwire("oauth_credentials_port", touched),
        google_tasks_oauth_service=Tripwire("google_tasks_oauth_service", touched),
        ms_todo_client_id="ms-client", ms_todo_client_secret="ms-secret",
        ms_todo_redirect_uri="https://app.example/auth/connect-microsoft-todo/callback",
        onedrive_redirect_uri="https://app.example/auth/connect-onedrive/callback",
        task_queue=Tripwire("task_queue", touched), connectors=ConnectorRegistry(),
    ))
    return app.test_client()


def _cookie(resp, name: str) -> str:
    for header in resp.headers.get_all("Set-Cookie"):
        if header.startswith(f"{name}="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"no Set-Cookie for {name}")


async def _login(client, monkeypatch):
    _pin(monkeypatch, T0)
    client.set_cookie("localhost", "oauth_state", "s1")
    resp = await client.get("/auth/callback?code=c&state=s1")
    assert resp.status_code == 302
    return _cookie(resp, "access_token"), _cookie(resp, "refresh_token")


async def _me(client, token: str) -> int:
    client.set_cookie("localhost", "access_token", token)
    return (await client.get("/auth/me")).status_code


async def _refresh(client, refresh: str):
    client.set_cookie("localhost", "refresh_token", refresh)
    return await client.post("/auth/refresh")


class TestLoginTokensStopAtTwentyFourHours:
    async def test_access_token_is_accepted_just_before_and_refused_just_after_24_h(self, monkeypatch):
        client = await _world()
        access, _ = await _login(client, monkeypatch)
        _pin(monkeypatch, T0 + timedelta(seconds=DAY - 1))
        assert await _me(client, access) == 200
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        assert await _me(client, access) == 401

    async def test_refresh_token_is_refused_after_24_h(self, monkeypatch):
        client = await _world()
        _, refresh = await _login(client, monkeypatch)
        _pin(monkeypatch, T0 + timedelta(seconds=DAY - 1))
        assert (await _refresh(client, refresh)).status_code == 200
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        resp = await _refresh(client, refresh)
        assert resp.status_code == 401
        assert "access_token" not in await resp.get_json()

    async def test_older_longer_lived_refresh_token_is_refused_by_the_age_check_alone(self, monkeypatch):
        """A refresh token minted under a 30 day TTL has an `exp` far in the future; only the `iat` age check against the
        current 24 h TTL (session_service.py:228-232) can refuse it at t0 + 24 h + 1 s."""
        client = await _world()
        await _login(client, monkeypatch)  # pins t0
        old = SessionService(secret_key=SESSION_SECRET, access_token_ttl=DAY, refresh_token_ttl=30 * DAY)
        long_lived = old.create_refresh_token(UserProfile(user_id="u1", account_id="a1"), BillingAccount(account_id="a1"))
        _pin(monkeypatch, T0 + timedelta(seconds=DAY - 1))
        assert (await _refresh(client, long_lived)).status_code == 200
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        resp = await _refresh(client, long_lived)
        assert resp.status_code == 401
        assert "access_token" not in await resp.get_json()


class TestRefreshDoesNotExtendTheSession:
    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="P16-D6: /auth/refresh mints a fresh 24 h access token (oauth_app.py:291), so a token "
                              "refreshed at 23h59m lives to ~48h; the 24 h cap is not enforced on it; owner decision")
    async def test_access_token_minted_by_refresh_is_refused_after_the_logins_24_h(self, monkeypatch):
        client = await _world()
        _, refresh = await _login(client, monkeypatch)
        _pin(monkeypatch, T0 + timedelta(hours=23, minutes=59))
        resp = await _refresh(client, refresh)
        assert resp.status_code == 200
        renewed = (await resp.get_json())["access_token"]
        _pin(monkeypatch, T0 + timedelta(seconds=DAY + 1))
        assert await _me(client, renewed) == 401
