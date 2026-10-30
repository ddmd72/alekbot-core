"""Refusals of the OAuth blueprint, route by route (P7, inventory §4; `src/web/oauth_app.py`).

Every gate is real: the session service verifies real JWTs, AuthenticationService runs over the real user, account and
whitelist repositories on the evaluating Firestore fake. Only Google (the `AuthPort` boundary) is scripted. A refused
request must leave no session cookie, no state cookie, no stored credential and no changed user document behind.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.web.oauth_app import create_oauth_blueprint
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import (
    Tripwire, access_token, expired_token, forged_token, refresh_token, session_service,
)
from src.composition.connector_registry import ConnectorRegistry

ALLOWED = "owner@example.com"
USERS, ACCOUNTS, WHITELIST = "users", "accounts", "whitelist"


class GoogleStub(AuthPort):
    """The provider boundary, scripted: a code exchange and an ID-token check that succeed or fail on demand."""

    def __init__(self, exchange_fails: bool = False, verify_fails: bool = False, sub: str = "sub-1") -> None:
        self.exchange_fails, self.verify_fails, self.sub = exchange_fails, verify_fails, sub

    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        if self.exchange_fails:
            raise RuntimeError("invalid_grant")
        return OAuthTokens(access_token="g-at", id_token="g-id", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        if self.verify_fails:
            raise ValueError("Token signature verification failed")
        now = datetime.now(timezone.utc)
        return TokenClaims(sub=self.sub, iss="https://accounts.google.com", aud="client", exp=now + timedelta(hours=1),
                           iat=now, email=ALLOWED, email_verified=True)

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub=self.sub, email=ALLOWED, email_verified=True, name="Owner")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


class World:
    """The real blueprint with real services on one fake Firestore."""

    def __init__(self, provider: Optional[GoogleStub] = None) -> None:
        self.db = EvalDb()
        self.touched: list = []
        env = SimpleNamespace(domain_users_collection=USERS, domain_whitelist_collection=WHITELIST,
                              is_production=False)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        registry = AuthProviderRegistry({"google": provider or GoogleStub()}, default_provider_name="google")
        self.auth = AuthenticationService(registry, self.users, self.accounts,
                                          whitelist_repo=FirestoreWhitelistRepository(self.db, env))
        self.credentials = Tripwire("oauth_credentials_port", self.touched)
        self.task_queue = Tripwire("task_queue", self.touched)
        self.app = Quart("oauth_refusals")
        self.app.register_blueprint(create_oauth_blueprint(
            auth_service=self.auth,
            session_service=session_service(),
            auth_registry=registry,
            auth_config=SimpleNamespace(
                oauth_redirect_uri="https://app.example/auth/callback",
                gmail_oauth_redirect_uri="https://app.example/auth/connect-gmail/callback",
                google_tasks_oauth_redirect_uri="https://app.example/auth/connect-google-tasks/callback",
                access_token_ttl=86400, refresh_token_ttl=86400,
            ),
            connect_service=Tripwire("connect_service", self.touched), connectors=ConnectorRegistry(),
        ))
        self.client = self.app.test_client()

    async def seed(self) -> None:
        await self.db.collection(WHITELIST).document("config").set({"allowed_emails": [ALLOWED], "allowed_domains": []})
        for uid, acc in (("user-a", "account-a"), ("user-b", "account-b")):
            await self.accounts.create_account(BillingAccount(account_id=acc))
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc))

    def user_docs(self) -> dict:
        return {k: dict(v) for k, v in self.db.collection(USERS).docs.items()}


def _set_cookies(resp) -> str:
    return " ".join(resp.headers.get_all("Set-Cookie"))


def _no_session_cookies(resp) -> bool:
    cookies = _set_cookies(resp)
    return "access_token=" not in cookies and "refresh_token=" not in cookies


# --------------------------------------------------------------------------- /auth/callback

class TestCallback:
    async def test_missing_code_is_400_without_session(self):
        w = World()
        w.client.set_cookie("localhost", "oauth_state", "s1")
        resp = await w.client.get("/auth/callback?state=s1")
        assert resp.status_code == 400
        assert _no_session_cookies(resp)
        assert w.user_docs() == {}

    @pytest.mark.parametrize("cookie_state", [None, "other-state"])
    async def test_absent_or_different_state_cookie_is_400_without_session(self, cookie_state):
        w = World()
        await w.seed()
        before = w.user_docs()
        if cookie_state is not None:
            w.client.set_cookie("localhost", "oauth_state", cookie_state)
        resp = await w.client.get("/auth/callback?code=c&state=s1")
        assert resp.status_code == 400
        assert (await resp.get_json())["error"] == "Invalid state parameter"
        assert _no_session_cookies(resp)
        assert w.user_docs() == before

    async def test_sign_in_never_redirects_off_site(self):
        w = World()
        await w.seed()
        await w.users.update_user(UserProfile(user_id="user-a", account_id="account-a", external_user_id="google|sub-1"))
        login = await w.client.get("/auth/login?next=https://evil.example/phish")
        assert login.status_code == 302
        stored = {c.name: c.value for c in w.client.cookie_jar}  # what login set; the browser replays it
        resp = await w.client.get(f"/auth/callback?code=c&state={stored['oauth_state']}")

        assert resp.status_code == 302 and "access_token=" in _set_cookies(resp)  # the sign-in itself succeeded
        target = resp.headers["Location"]
        assert target.startswith("/") and not target.startswith("//"), target


# --------------------------------------------------------------------------- /auth/refresh

class TestRefresh:
    async def test_no_token_is_401(self):
        resp = await World().client.post("/auth/refresh")
        assert resp.status_code == 401
        assert "access_token=" not in _set_cookies(resp)

    async def test_expired_refresh_token_is_401(self):
        w = World()
        w.client.set_cookie("localhost", "refresh_token", expired_token("refresh"))
        resp = await w.client.post("/auth/refresh")
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Refresh token expired"
        assert "access_token=" not in _set_cookies(resp)

    async def test_forged_refresh_token_is_401(self):
        w = World()
        resp = await w.client.post("/auth/refresh", headers={"Authorization": f"Bearer {forged_token('refresh')}"})
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Invalid refresh token"
        assert "access_token=" not in _set_cookies(resp)

    async def test_access_token_presented_as_refresh_issues_nothing(self):
        # Refused, but as a 500: verify_refresh_token raises ValueError for the wrong type, which the route's
        # jwt handlers do not catch (src/web/oauth_app.py:311-320; session_service.py:225-226). P7 report item.
        w = World()
        w.client.set_cookie("localhost", "refresh_token", access_token())
        resp = await w.client.post("/auth/refresh")
        assert resp.status_code >= 400
        body = await resp.get_json()
        assert "access_token" not in body
        assert "access_token=" not in _set_cookies(resp)

    @pytest.mark.xfail(strict=True, raises=AssertionError,
                       reason="known defect: an access token presented as refresh is a client error answered 500 + "
                              "ERROR log; verify_refresh_token raises ValueError (session_service.py:225-226), which "
                              "oauth_app.py:318-320 turns into 500")
    async def test_access_token_presented_as_refresh_is_401(self):
        w = World()
        w.client.set_cookie("localhost", "refresh_token", access_token())
        resp = await w.client.post("/auth/refresh")
        assert resp.status_code == 401

    async def test_valid_refresh_token_is_accepted(self):
        # The positive control: the refusals above are not a route that refuses everything.
        w = World()
        w.client.set_cookie("localhost", "refresh_token", refresh_token())
        resp = await w.client.post("/auth/refresh")
        assert resp.status_code == 200
        assert session_service().verify_access_token((await resp.get_json())["access_token"])["sub"] == "user-a"


# --------------------------------------------------------------------------- /auth/me

class TestMe:
    async def test_no_token_is_401(self):
        resp = await World().client.get("/auth/me")
        assert resp.status_code == 401

    async def test_expired_token_is_401(self):
        w = World()
        w.client.set_cookie("localhost", "access_token", expired_token())
        resp = await w.client.get("/auth/me")
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Token expired"

    async def test_forged_token_is_401(self):
        w = World()
        resp = await w.client.get("/auth/me", headers={"Authorization": f"Bearer {forged_token()}"})
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == "Invalid token"

    async def test_valid_token_is_answered(self):
        w = World()
        resp = await w.client.get("/auth/me", headers={"Authorization": f"Bearer {access_token()}"})
        assert resp.status_code == 200
        assert (await resp.get_json())["user"]["user_id"] == "user-a"


# --------------------------------------------------------------------------- /auth/link-oauth

class TestLinkOauth:
    async def _post(self, w: World, *, token: Optional[str] = None, body: Optional[dict] = None,
                    state_cookie: Optional[str] = "s1"):
        if state_cookie is not None:
            w.client.set_cookie("localhost", "oauth_state", state_cookie)
        if token is not None:
            w.client.set_cookie("localhost", "access_token", token)
        return await w.client.post("/auth/link-oauth", json=body if body is not None else {"code": "c", "state": "s1"})

    async def test_not_signed_in_is_401_and_links_nothing(self):
        w = World()
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w)
        assert resp.status_code == 401
        assert w.user_docs() == before

    async def test_missing_code_is_400(self):
        w = World()
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w, token=access_token(), body={"state": "s1"})
        assert resp.status_code == 400
        assert w.user_docs() == before

    @pytest.mark.parametrize("cookie_state", [None, "other-state"])
    async def test_bad_state_is_400(self, cookie_state):
        w = World()
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w, token=access_token(), state_cookie=cookie_state)
        assert resp.status_code == 400
        assert (await resp.get_json())["error"] == "Invalid state parameter"
        assert w.user_docs() == before

    @pytest.mark.parametrize("token,error", [
        (expired_token(), "Access token expired. Please login again."),
        (forged_token(), "Invalid access token"),
    ], ids=["expired", "forged"])
    async def test_expired_or_invalid_access_token_is_401(self, token, error):
        w = World()
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w, token=token)
        assert resp.status_code == 401
        assert (await resp.get_json())["error"] == error
        assert w.user_docs() == before

    async def test_identity_linked_to_another_user_is_400_and_neither_user_changes(self):
        w = World()
        await w.seed()
        await w.users.update_user(UserProfile(user_id="user-b", account_id="account-b", external_user_id="google|sub-1"))
        before = w.user_docs()
        resp = await self._post(w, token=access_token("user-a", "account-a"))
        assert resp.status_code == 400
        assert "already linked" in (await resp.get_json())["error"]
        assert w.user_docs() == before

    async def test_id_token_verification_failure_is_400_and_links_nothing(self):
        # authentication_service.py:441-446, the RFC's named site.
        w = World(GoogleStub(verify_fails=True))
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w, token=access_token())
        assert resp.status_code == 400
        assert (await resp.get_json())["error"].startswith("Failed to verify ID token")
        assert w.user_docs() == before

    async def test_code_exchange_failure_is_400_and_links_nothing(self):
        # authentication_service.py:433-437.
        w = World(GoogleStub(exchange_fails=True))
        await w.seed()
        before = w.user_docs()
        resp = await self._post(w, token=access_token())
        assert resp.status_code == 400
        assert (await resp.get_json())["error"].startswith("Failed to exchange authorization code")
        assert w.user_docs() == before

    async def test_a_valid_link_is_stored(self):
        # The positive control for the refusals above.
        w = World()
        await w.seed()
        resp = await self._post(w, token=access_token("user-a", "account-a"))
        assert resp.status_code == 200
        assert w.user_docs()["user-a"]["external_user_id"] == "google|sub-1"


# --------------------------------------------------------------------------- /auth/connect-*

_CONNECT = {
    "/auth/connect-gmail": "gmail_oauth_state",
    "/auth/connect-google-tasks": "tasks_oauth_state",
    "/auth/connect-microsoft-todo": "microsoft_todo_oauth_state",
    "/auth/connect-onedrive": "drive_oauth_state",
}


class TestConnect:
    @pytest.mark.parametrize("path", sorted(_CONNECT))
    @pytest.mark.parametrize("cookie", [None, "forged", "expired"])
    async def test_without_a_valid_session_redirects_to_login_and_sets_no_state(self, path, cookie):
        w = World()
        if cookie == "forged":
            w.client.set_cookie("localhost", "access_token", forged_token())
        elif cookie == "expired":
            w.client.set_cookie("localhost", "access_token", expired_token())
        resp = await w.client.get(path)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/auth/login?next=/cabinet"
        cookies = _set_cookies(resp)
        assert _CONNECT[path] not in cookies and "connect_user_id" not in cookies
        assert w.touched == []


_CALLBACKS = {
    "/auth/connect-gmail/callback": ("gmail_oauth_state", "gmail_connect_user_id", "gmail_error=state"),
    "/auth/connect-google-tasks/callback": ("tasks_oauth_state", "tasks_connect_user_id", "tasks_error=state"),
    "/auth/connect-microsoft-todo/callback": ("microsoft_todo_oauth_state", "microsoft_todo_connect_user_id",
                                              "microsoft_todo_error=state"),
}


class TestConnectCallback:
    @pytest.mark.parametrize("path", sorted(_CALLBACKS))
    @pytest.mark.parametrize("state_cookie,user_cookie", [
        (None, "user-a"),          # state cookie missing
        ("other-state", "user-a"),  # state mismatch
        ("s1", None),              # no *_connect_user_id
    ])
    async def test_bad_state_or_missing_user_saves_nothing(self, path, state_cookie, user_cookie):
        w = World()
        state_name, user_name, error = _CALLBACKS[path]
        if state_cookie is not None:
            w.client.set_cookie("localhost", state_name, state_cookie)
        if user_cookie is not None:
            w.client.set_cookie("localhost", user_name, user_cookie)
        resp = await w.client.get(f"{path}?code=c&state=s1")
        assert resp.status_code == 302
        assert resp.headers["Location"] == f"/cabinet?{error}"
        assert w.touched == []   # no code exchange, no credential saved, no setup task
