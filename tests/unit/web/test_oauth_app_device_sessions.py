"""The OAuth blueprint over device sessions (CABINET_DEVICE_SESSIONS_RFC §7.1, §7.4 server contract): the sign-in starts a
session and sets the two `__Host-` cookies, `/auth/refresh` rotates (or, for a second tab, only re-mints) and refuses a
replayed or ended session, `/auth/logout` revokes, `/auth/me` reports the access expiry.

A harness of its own: the user, account and whitelist repositories on the evaluating Firestore fake with the real
`AuthenticationService`, and the device sessions of `CabinetSessionKit` (real `DeviceSessionService` over the
repository twin, a controlled clock) behind the real verifier. Only Google (the `AuthPort` boundary) is scripted.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import SessionAuthKind, SessionRevokeReason
from src.domain.device_session_policy import DeviceSessionPolicy, refresh_secret_hash
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE
from src.web.oauth_app import create_oauth_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.unit.web.gate_fakes import Tripwire

USERS, ACCOUNTS, WHITELIST = "users", "accounts", "whitelist"
ALEX = "alex@example.com"
IPHONE_HOME_SCREEN = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Mobile/15E148")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")


class _Google(AuthPort):
    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="tok-test-1", id_token="id-test-1", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        now = datetime.now(timezone.utc)
        return TokenClaims(sub="sub-alex", iss="https://accounts.google.com", aud="client",
                           exp=now + timedelta(hours=1), iat=now, email=ALEX, email_verified=True)

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub="sub-alex", email=ALEX, email_verified=True, name="Alex")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


class UsersDuringAReadOutage(FirestoreUserRepository):
    outage = False

    async def get_user(self, user_id: str):
        if self.outage:
            raise ServiceUnavailable("firestore unavailable")
        return await super().get_user(user_id)


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, domain_whitelist_collection=WHITELIST,
                              is_production=False)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = UsersDuringAReadOutage(self.db, env, self.accounts)
        registry = AuthProviderRegistry({"google": _Google()}, default_provider_name="google")
        auth = AuthenticationService(registry, self.users, self.accounts,
                                     whitelist_repo=FirestoreWhitelistRepository(self.db, env))
        self.kit = CabinetSessionKit()
        app = Quart("oauth_device_sessions")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=auth, cabinet_sessions=self.kit.verifier, device_sessions=self.kit.device_sessions,
            auth_registry=registry, auth_config=SimpleNamespace(oauth_redirect_uri="https://app.example/auth/callback"),
            connect_service=Tripwire("connect_service", []), connectors=ConnectorRegistry()))
        self.client = app.test_client()

    async def allow(self, *emails: str) -> None:
        await self.db.collection(WHITELIST).document("config").set({"allowed_emails": list(emails),
                                                                    "allowed_domains": []})

    async def sign_in(self, user_agent: str = MAC_CHROME, next_path: str = "/cabinet"):
        self.client.set_cookie("localhost", "oauth_state", "s1")
        self.client.set_cookie("localhost", "auth_next", next_path)
        resp = await self.client.get("/auth/callback?code=c1&state=s1", headers={"User-Agent": user_agent})
        assert resp.status_code == 302, "the sign-in succeeds"
        return resp

    def present(self, cookies: dict) -> None:
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            if cookies.get(name):
                self.client.set_cookie("localhost", name, cookies[name])

    async def refresh(self, cookies: dict):
        self.present(cookies)
        return await self.client.post("/auth/refresh")

    def only_session(self):
        (session,) = self.kit.repo.sessions.values()
        return session

    def user_doc(self) -> dict:
        (user,) = self.db.collection(USERS).docs.values()
        return user

    def claims(self, token: str) -> dict:
        return self.kit.tokens.verify_device_access_token(token, allow_expired=True)


def _set_cookies(resp) -> dict:
    """name -> the whole Set-Cookie header."""
    return {h.split("=", 1)[0]: h for h in resp.headers.get_all("Set-Cookie")}


def _values(resp) -> dict:
    return {name: header.split("=", 1)[1].split(";", 1)[0] for name, header in _set_cookies(resp).items()}


def _cleared(resp) -> bool:
    headers = _set_cookies(resp)
    return all(name in headers and headers[name].startswith(f"{name}=;") and "Max-Age=0" in headers[name]
               for name in (ACCESS_COOKIE, REFRESH_COOKIE))


@pytest.fixture(autouse=True)
def _transactions():
    with eval_transactions():
        yield


@pytest.fixture
async def world():
    w = World()
    await w.allow(ALEX)
    return w


async def _signed_in_minutes_ago(world, minutes: float, **sign_in) -> dict:
    """Sign in with the session clock `minutes` in the past, then bring it back to now: the access JWT is expired."""
    now = world.kit.clock.now
    world.kit.clock.set(now - timedelta(minutes=minutes))
    cookies = _values(await world.sign_in(**sign_in))
    world.kit.clock.set(now)
    return cookies


# ---------------------------------------------------------------------------------------------- sign-in

class TestSignIn:
    async def test_it_starts_a_session_for_the_signed_in_user_and_sets_both_host_cookies(self, world):
        resp = await world.sign_in(user_agent=IPHONE_HOME_SCREEN, next_path="/cabinet/call")
        assert resp.headers["Location"] == "/cabinet/call"
        session, user = world.only_session(), world.user_doc()
        assert (session.user_id, session.account_id) == (user["user_id"], user["account_id"])
        assert (session.label, session.platform_hint) == ("iPhone — Home Screen (call)", "iPhone — Home Screen")
        assert session.auth_kind is SessionAuthKind.GOOGLE
        cookies, headers = _values(resp), _set_cookies(resp)
        assert world.claims(cookies[ACCESS_COOKIE])["sid"] == session.sid
        assert refresh_secret_hash(cookies[REFRESH_COOKIE]) == session.refresh_hash
        lifetime = int(DeviceSessionPolicy.ABSOLUTE_TTL.total_seconds())
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={lifetime}"):
                assert flag in headers[name]
        assert "access_token" not in cookies and "refresh_token" not in cookies

    async def test_the_new_cookie_opens_the_cabinet_session(self, world):
        cookies = _values(await world.sign_in())
        world.present(cookies)
        resp = await world.client.get("/auth/me")
        assert resp.status_code == 200 and (await resp.get_json())["user"]["email"] == ALEX

    async def test_a_refused_sign_in_starts_no_session(self, world):
        await world.allow("someone-else@example.com")
        world.client.set_cookie("localhost", "oauth_state", "s1")
        resp = await world.client.get("/auth/callback?code=c1&state=s1")
        assert resp.status_code == 403
        assert world.kit.repo.sessions == {} and ACCESS_COOKIE not in _set_cookies(resp)


# ---------------------------------------------------------------------------------------------- refresh

class TestRefresh:
    async def test_an_expired_access_cookie_is_renewed_and_the_secret_rotates(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        assert world.claims(cookies[ACCESS_COOKIE])["exp"] < datetime.now(timezone.utc).timestamp(), "expired"
        resp = await world.refresh(cookies)
        assert resp.status_code == 200
        new = _values(resp)
        assert set(new) == {ACCESS_COOKIE, REFRESH_COOKIE}
        assert new[REFRESH_COOKIE] != cookies[REFRESH_COOKIE]
        session = world.only_session()
        assert session.refresh_hash == refresh_secret_hash(new[REFRESH_COOKIE])
        claims = world.claims(new[ACCESS_COOKIE])
        assert claims["sid"] == session.sid
        assert await resp.get_json() == {"success": True, "expires_at": claims["exp"]}
        world.present(new)
        assert (await world.client.get("/auth/me")).status_code == 200

    async def test_a_second_tab_within_the_grace_gets_an_access_cookie_only(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        rotated = await world.refresh(cookies)
        world.kit.clock.advance(seconds=30)
        resp = await world.refresh(cookies)                       # the other tab still holds the old secret
        assert rotated.status_code == 200 and resp.status_code == 200
        assert set(_values(resp)) == {ACCESS_COOKIE}
        assert world.only_session().refresh_hash == refresh_secret_hash(_values(rotated)[REFRESH_COOKIE])

    async def test_an_old_secret_after_the_grace_revokes_the_session(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        newest = _values(await world.refresh(cookies))
        world.kit.clock.advance(seconds=DeviceSessionPolicy.ROTATION_GRACE.total_seconds() + 1)
        resp = await world.refresh(cookies)
        assert resp.status_code == 401 and _cleared(resp)
        assert world.only_session().revoke_reason is SessionRevokeReason.REPLAY
        me = await world.client.get("/auth/me", headers={"Authorization": f"Bearer {newest[ACCESS_COOKIE]}"})
        assert me.status_code == 401                              # the rightful holder is shut out too

    async def test_a_logged_out_session_cannot_refresh(self, world):
        cookies = _values(await world.sign_in())
        world.present(cookies)
        await world.client.post("/auth/logout")
        resp = await world.refresh(cookies)
        assert resp.status_code == 401 and _cleared(resp)

    async def test_a_session_past_its_idle_end_cannot_refresh(self, world):
        cookies = _values(await world.sign_in())
        world.kit.clock.advance(days=14, seconds=1)
        resp = await world.refresh(cookies)
        assert resp.status_code == 401 and _cleared(resp)

    @pytest.mark.parametrize("drop", [ACCESS_COOKIE, REFRESH_COOKIE])
    async def test_without_either_cookie_it_is_401_and_clears_both(self, world, drop):
        cookies = _values(await world.sign_in())
        world.client = world.client.app.test_client()             # a clean cookie jar
        resp = await world.refresh({**cookies, drop: ""})
        assert resp.status_code == 401 and _cleared(resp)

    async def test_a_forged_access_cookie_is_401(self, world):
        cookies = _values(await world.sign_in())
        resp = await world.refresh({**cookies, ACCESS_COOKIE: cookies[ACCESS_COOKIE][:-4] + "AAAA"})
        assert resp.status_code == 401 and _cleared(resp)

    async def test_role_tier_and_account_come_from_the_store_not_from_the_old_token(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        user = world.user_doc()
        # The user moved to another account (a team invite) after signing in: the renewal is derived from the store.
        await world.accounts.create_account(BillingAccount(account_id="account-b", tier=AccountTier.PRO,
                                                           iam_policy={user["user_id"]: "member"}))
        await world.db.collection(USERS).document(user["user_id"]).update({"account_id": "account-b"})
        resp = await world.refresh(cookies)
        assert resp.status_code == 200
        claims = world.claims(_values(resp)[ACCESS_COOKIE])
        assert (claims["sub"], claims["account_id"], claims["tier"], claims["role"]) == (
            user["user_id"], "account-b", "pro", "member")

    async def test_the_owner_role_survives_a_refresh(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        assert world.claims(cookies[ACCESS_COOKIE])["role"] == "owner"
        resp = await world.refresh(cookies)
        assert world.claims(_values(resp)[ACCESS_COOKIE])["role"] == "owner"

    @pytest.mark.parametrize("change", ["inactive", "off the whitelist", "deleted"])
    async def test_a_user_resume_session_refuses_gets_401_no_access_and_the_secret_stays(self, world, change):
        cookies = await _signed_in_minutes_ago(world, 20)
        user_id = world.user_doc()["user_id"]
        if change == "inactive":
            await world.db.collection(USERS).document(user_id).update({"is_active": False})
        elif change == "off the whitelist":
            await world.allow("someone-else@example.com")
        else:
            await world.users.delete_user(user_id)
        before = world.only_session()
        resp = await world.refresh(cookies)
        assert resp.status_code == 401
        assert await resp.get_json() == {"error": "Session no longer valid"}
        assert _cleared(resp) and world.only_session() == before

    async def test_a_store_outage_is_500_with_no_cookie_change_and_no_detail(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        world.users.outage = True
        resp = await world.refresh(cookies)
        assert resp.status_code == 500
        assert await resp.get_json() == {"error": "Token refresh failed"}
        assert _set_cookies(resp) == {}


# ---------------------------------------------------------------------------------------------- logout and me

class TestLogout:
    async def test_it_revokes_the_session_clears_both_cookies_and_changes_no_user_or_account(self, world):
        cookies = _values(await world.sign_in())
        before = {c: dict(world.db.collection(c).docs) for c in (USERS, ACCOUNTS)}
        world.present(cookies)
        resp = await world.client.post("/auth/logout")
        assert resp.status_code == 200 and _cleared(resp)
        assert world.only_session().revoke_reason is SessionRevokeReason.LOGOUT
        assert {c: world.db.collection(c).docs for c in (USERS, ACCOUNTS)} == before
        me = await world.client.get("/auth/me", headers={"Authorization": f"Bearer {cookies[ACCESS_COOKIE]}"})
        assert me.status_code == 401

    async def test_an_expired_access_cookie_still_revokes(self, world):
        cookies = await _signed_in_minutes_ago(world, 20)
        world.present(cookies)
        await world.client.post("/auth/logout")
        assert world.only_session().revoke_reason is SessionRevokeReason.LOGOUT

    async def test_without_a_session_it_only_clears_the_cookies(self, world):
        resp = await world.client.post("/auth/logout")
        assert resp.status_code == 200 and _cleared(resp)

    async def test_a_failed_revocation_still_clears_the_cookies_and_says_so(self, world):
        cookies = _values(await world.sign_in())

        async def outage(*args, **kwargs):
            raise ServiceUnavailable("firestore unavailable")
        world.kit.repo.revoke = outage    # type: ignore[method-assign]
        world.present(cookies)
        resp = await world.client.post("/auth/logout")
        assert resp.status_code == 500 and _cleared(resp)
        assert await resp.get_json() == {"error": "Logout failed"}


class TestMe:
    async def test_it_answers_for_the_live_session_with_the_access_expiry(self, world):
        cookies = _values(await world.sign_in())
        user = world.user_doc()
        resp = await world.client.get("/auth/me", headers={"Authorization": f"Bearer {cookies[ACCESS_COOKIE]}"})
        body = await resp.get_json()
        assert body["user"] == {"user_id": user["user_id"], "email": ALEX, "external_user_id": user["external_user_id"]}
        assert body["account"] == {"account_id": user["account_id"], "tier": "free", "role": "owner"}
        assert body["expires_at"] == world.claims(cookies[ACCESS_COOKIE])["exp"]
        assert isinstance(body["expires_at"], int)

    async def test_without_a_live_session_it_is_401(self, world):
        resp = await world.client.get("/auth/me")
        assert resp.status_code == 401 and await resp.get_json() == {"error": "Not authenticated"}
