"""The OAuth blueprint's happy paths and what they store (P19), complementing the refusal tests of P7 and the
connect-flow tests (`test_connect_routes*.py`): sign-in start, a successful callback (new user, cookies, the invite code),
logout, refresh and `/auth/me` for the right tenant.

Real device-session kit, `AuthenticationService`, user/account/whitelist/invite repositories on the evaluating fake; only
Google (the `AuthPort` boundary) is scripted.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_invite_code_repo import FirestoreInviteCodeRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.domain.device_session_policy import refresh_secret_hash
from src.services.invite_code_service import InviteCodeService
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE
from src.web.oauth_app import create_oauth_blueprint
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.support.redelivery_world import require
from tests.unit.web.gate_fakes import KIT, Tripwire

USERS, ACCOUNTS, WHITELIST, INVITES = "users", "accounts", "whitelist", "invites"
ALEX = "alex@example.com"
CLARA = "clara@example.com"


class _Google(AuthPort):
    def __init__(self) -> None:
        self.sub, self.email = "sub-alex", ALEX

    def get_provider_name(self) -> str:
        return "google"

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        return f"https://accounts.example/auth?state={state}&redirect_uri={redirect_uri}"

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> OAuthTokens:
        return OAuthTokens(access_token="tok-test-1", id_token="id-test-1", expires_in=3600)

    async def verify_token(self, id_token: str) -> TokenClaims:
        now = datetime.now(timezone.utc)
        return TokenClaims(sub=self.sub, iss="https://accounts.google.com", aud="client", exp=now + timedelta(hours=1),
                           iat=now, email=self.email, email_verified=True)

    async def get_user_info(self, access_token: str) -> OAuthUserInfo:
        return OAuthUserInfo(sub=self.sub, email=self.email, email_verified=True, name="Alex")

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        raise NotImplementedError


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.provider = _Google()
        env = SimpleNamespace(domain_users_collection=USERS, domain_whitelist_collection=WHITELIST,
                              domain_invite_codes_collection=INVITES, is_production=False)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        whitelist = FirestoreWhitelistRepository(self.db, env)
        registry = AuthProviderRegistry({"google": self.provider}, default_provider_name="google")
        self.auth = AuthenticationService(registry, self.users, self.accounts, whitelist_repo=whitelist)
        self.invites = FirestoreInviteCodeRepository(self.db, env, digest_key=b"k" * 32)
        self.invite_service = InviteCodeService(self.invites, self.users, self.accounts, whitelist)
        self.kit = KIT
        app = Quart("oauth_routes")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=self.auth, cabinet_sessions=self.kit.verifier, device_sessions=self.kit.device_sessions,
            auth_registry=registry,
            auth_config=SimpleNamespace(oauth_redirect_uri="https://app.example/auth/callback"),
            invite_service=self.invite_service, connect_service=Tripwire("connect_service", []),
            connectors=ConnectorRegistry()))
        self.client = app.test_client()

    async def allow(self, *emails: str) -> None:
        await self.db.collection(WHITELIST).document("config").set({"allowed_emails": list(emails), "allowed_domains": []})

    async def sign_in(self, **cookies):
        for k, v in {"oauth_state": "s1", **cookies}.items():
            self.client.set_cookie("localhost", k, v)
        return await self.client.get("/auth/callback?code=c1&state=s1")


def _cookies(resp) -> dict:
    out = {}
    for header in resp.headers.get_all("Set-Cookie"):
        name, _, rest = header.partition("=")
        out[name] = rest.split(";", 1)[0]
    return out


@pytest.fixture(autouse=True)
def _transactions():
    with eval_transactions():
        yield


@pytest.fixture
def world():
    return World()


class TestLogin:
    async def test_it_redirects_to_the_provider_with_a_fresh_state_stored_in_a_cookie(self, world):
        a = await world.client.get("/auth/login/google")
        b = await world.client.get("/auth/login/google")
        state_a = parse_qs(urlparse(a.headers["Location"]).query)["state"][0]
        assert a.status_code == 302 and _cookies(a)["oauth_state"] == state_a
        assert _cookies(b)["oauth_state"] != state_a
        assert len(state_a) >= 32
        assert "Secure" in a.headers.get_all("Set-Cookie")[0] and "HttpOnly" in a.headers.get_all("Set-Cookie")[0]

    async def test_next_and_invite_code_are_carried_in_cookies_and_an_off_site_next_is_dropped_to_the_default(self, world):
        ok = await world.client.get("/auth/login/google?next=/cabinet&code=INV-1")
        assert _cookies(ok)["auth_next"] in ("/cabinet", "%2Fcabinet")
        assert _cookies(ok)["invite_code"] == "INV-1"
        evil = await world.client.get("/auth/login/google?next=https://evil.example/x")
        assert "evil.example" not in _cookies(evil).get("auth_next", "")

    async def test_an_unknown_provider_is_500_and_sets_no_state(self, world):
        resp = await world.client.get("/auth/login?provider=nope")
        assert resp.status_code == 500 and "oauth_state" not in _cookies(resp)


class TestCallbackSuccess:
    async def test_a_whitelisted_first_sign_in_creates_the_user_and_account_and_sets_session_cookies(self, world):
        await world.allow(ALEX)
        resp = await world.sign_in(auth_next="/cabinet")
        assert resp.status_code == 302 and resp.headers["Location"] == "/cabinet"
        (user,) = world.db.collection(USERS).docs.values()
        assert user["email"] == ALEX and user["external_user_id"] == "google|sub-alex"
        cookies = _cookies(resp)
        access = world.kit.tokens.verify_device_access_token(cookies[ACCESS_COOKIE])
        assert (access["sub"], access["account_id"]) == (user["user_id"], user["account_id"])
        stored = world.kit.repo.sessions[access["sid"]]       # the refresh cookie is the secret of the user's session
        assert stored.user_id == user["user_id"] and stored.refresh_hash == refresh_secret_hash(cookies[REFRESH_COOKIE])
        assert user["account_id"] in world.db.collection(ACCOUNTS).docs
        assert cookies["oauth_state"] == ""                                    # the CSRF cookie is cleared

    async def test_a_second_sign_in_reuses_the_same_user_and_account(self, world):
        await world.allow(ALEX)
        await world.sign_in()
        await world.sign_in()
        assert len(world.db.collection(USERS).docs) == 1 and len(world.db.collection(ACCOUNTS).docs) == 1

    async def test_the_session_names_the_user_of_the_provider_identity_not_another(self, world):
        await world.allow(ALEX, CLARA)
        first = await world.sign_in()
        world.provider.sub, world.provider.email = "sub-clara", CLARA
        second = await world.sign_in()
        ids = {world.kit.tokens.verify_device_access_token(_cookies(r)[ACCESS_COOKIE])["sub"] for r in (first, second)}
        assert len(ids) == 2 and len(world.db.collection(USERS).docs) == 2

    async def test_a_pending_invite_code_is_consumed_for_the_signing_in_user(self, world):
        await world.allow(ALEX, CLARA)
        await world.sign_in()
        (owner,) = world.db.collection(USERS).docs.values()
        invite = await world.invite_service.generate_team_invite(owner["user_id"], owner["account_id"], "MEMBER")
        world.provider.sub, world.provider.email = "sub-clara", CLARA
        resp = await world.sign_in(invite_code=invite.code)
        assert resp.status_code == 302
        clara = next(u for u in world.db.collection(USERS).docs.values() if u["email"] == CLARA)
        assert clara["account_id"] == owner["account_id"]

    async def test_a_bad_invite_code_does_not_block_the_sign_in(self, world):
        await world.allow(ALEX)
        resp = await world.sign_in(invite_code="NOT-A-CODE")
        assert resp.status_code == 302 and ACCESS_COOKIE in _cookies(resp)


