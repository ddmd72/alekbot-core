"""Passkeys over HTTP, end to end (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5, §13 decision 1): the sign-in page and its
Google path, passkey sign-in, enrolment and removal in Cabinet -> Devices, and the passkey a web call start needs.

A harness of its own: the real `AuthenticationService` over the user, account and whitelist repositories on the
evaluating Firestore fake (only Google, the `AuthPort` boundary, is scripted), the device sessions of
`CabinetSessionKit` behind the real verifier, the real `PasskeyService` with the real WebAuthn verifier for
`cabinet.example.com` and its challenges in the real `FirestoreEphemeralStore`, and a software authenticator
(`tests/support/webauthn_authenticator.py`) answering as the browser would. The oauth, devices and web-call blueprints
are the deployed ones. A first sign-in is always a Google one (that is how a user and a session first exist); every
passkey then goes through the routes.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from quart import Quart

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_whitelist_repo import FirestoreWhitelistRepository
from src.adapters.webauthn_signer_verifier import WebAuthnSignerVerifier
from src.composition.connector_registry import ConnectorRegistry
from src.domain.auth import OAuthTokens, OAuthUserInfo, TokenClaims
from src.domain.device_session import SessionAuthKind, SessionRevokeReason
from src.domain.ui_messages import UIMessage
from src.ports.auth_port import AuthPort
from src.services.auth_provider_registry import AuthProviderRegistry
from src.services.authentication_service import AuthenticationService
from src.services.passkey_service import PasskeyService
from src.web.cabinet_auth import ACCESS_COOKIE, REFRESH_COOKIE
from src.web.device_auth_app import REGISTER_COOKIE, SIGN_IN_COOKIE, STEP_UP_COOKIE, create_device_auth_blueprint
from src.web.oauth_app import create_oauth_blueprint
from src.web.voice_web_call_app import create_voice_web_call_blueprint
from tests.support.cabinet_session_kit import CabinetSessionKit
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.support.webauthn_authenticator import SoftAuthenticator
from tests.unit.web.test_voice_web_call_app import CIPHER, MemoryStore, _room, _setup

USERS, ACCOUNTS, WHITELIST, CHALLENGES = "users", "accounts", "whitelist", "auth_passkey_challenges"
ALEX = "alex@example.com"
RP, ORIGIN = "cabinet.example.com", "https://cabinet.example.com"
IPHONE_HOME_SCREEN = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
                      "(KHTML, like Gecko) Mobile/15E148")
MAC_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
CALL = {"sdp": "OFFER", "mid": "0"}


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


def _set_cookies(resp) -> dict:
    """name -> the whole Set-Cookie header."""
    return {h.split("=", 1)[0]: h for h in resp.headers.get_all("Set-Cookie")}


def _values(resp) -> dict:
    return {name: header.split("=", 1)[1].split(";", 1)[0] for name, header in _set_cookies(resp).items()}


def _deleted(resp, name: str) -> bool:
    header = _set_cookies(resp).get(name, "")
    return header.startswith(f"{name}=;") and "Max-Age=0" in header


class World:
    def __init__(self, *, passkeys_wired: bool = True) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, domain_whitelist_collection=WHITELIST,
                              is_production=False)
        self.accounts = FirestoreAccountRepository(self.db, ACCOUNTS)
        self.users = FirestoreUserRepository(self.db, env, self.accounts)
        registry = AuthProviderRegistry({"google": _Google()}, default_provider_name="google")
        self.auth = AuthenticationService(registry, self.users, self.accounts,
                                          whitelist_repo=FirestoreWhitelistRepository(self.db, env))
        self.kit = CabinetSessionKit()
        self.passkeys = PasskeyService(self.kit.repo, WebAuthnSignerVerifier(rp_id=RP, rp_name="Alek", origin=ORIGIN),
                                       FirestoreEphemeralStore(self.db, CHALLENGES), self.kit.notices,
                                       clock=self.kit.clock)
        self.authenticator = SoftAuthenticator(RP, ORIGIN)
        self.calls = MemoryStore()
        app = Quart("device_auth_passkeys")
        app.register_blueprint(create_oauth_blueprint(
            auth_service=self.auth, cabinet_sessions=self.kit.verifier, device_sessions=self.kit.device_sessions,
            auth_registry=registry, auth_config=SimpleNamespace(oauth_redirect_uri=f"{ORIGIN}/auth/callback"),
            connectors=ConnectorRegistry()))
        app.register_blueprint(create_device_auth_blueprint(
            self.kit.verifier, self.kit.device_sessions, passkeys=self.passkeys if passkeys_wired else None,
            auth_service=self.auth if passkeys_wired else None))
        app.register_blueprint(create_voice_web_call_blueprint(
            cabinet_sessions=self.kit.verifier, call_setup=_setup(self.calls), media_room=_room(),
            ephemeral_store=self.calls, relay_base_url="wss://relay.example.com", cipher=CIPHER,
            digest_key=b"k" * 32, passkeys=self.passkeys if passkeys_wired else None))
        self.app = app
        self.client = app.test_client()
        self.jar: dict = {}

    # ---------------------------------------------------------------- browsers

    def browser(self):
        """A fresh cookie jar: another browser, or another Home Screen app."""
        self.client = self.app.test_client()
        self.jar = {}
        return self.client

    def keep(self, resp) -> None:
        """What the browser does with the response's cookies (the test jar does not send `Secure` ones over http)."""
        for name, value in _values(resp).items():
            if _deleted(resp, name):
                self.client.delete_cookie("localhost", name)
                self.jar.pop(name, None)
            else:
                self.client.set_cookie("localhost", name, value)
                self.jar[name] = value

    async def allow(self, *emails: str) -> None:
        await self.db.collection(WHITELIST).document("config").set({"allowed_emails": list(emails),
                                                                    "allowed_domains": []})

    async def google_sign_in(self, user_agent: str = MAC_CHROME, next_path: str = "/cabinet"):
        self.client.set_cookie("localhost", "oauth_state", "s1")
        self.client.set_cookie("localhost", "auth_next", next_path)
        resp = await self.client.get("/auth/callback?code=c1&state=s1", headers={"User-Agent": user_agent})
        assert resp.status_code == 302, "the Google sign-in succeeds"
        self.keep(resp)
        return resp

    def user_id(self) -> str:
        (user,) = self.db.collection(USERS).docs.values()
        return user["user_id"]

    def sid(self) -> str:
        """The sid of this browser's session."""
        return self.kit.tokens.verify_device_access_token(self.jar[ACCESS_COOKIE], allow_expired=True)["sid"]

    # ---------------------------------------------------------------- ceremonies over the routes

    async def post(self, path: str, body=None, **kwargs):
        resp = await self.client.post(path, json=body if body is not None else {}, **kwargs)
        self.keep(resp)
        return resp

    async def enrol(self, **register):
        options = await self.post("/api/user/passkeys/register/options")
        assert options.status_code == 200
        credential = self.authenticator.register((await options.get_json())["options"], **register)
        return await self.post("/api/user/passkeys/register/verify", {"credential": credential})

    async def passkey_sign_in(self, next_path="/cabinet/call", user_agent=IPHONE_HOME_SCREEN, **assertion):
        options = await self.post("/auth/passkey/options")
        assert options.status_code == 200
        credential = self.authenticator.sign_in((await options.get_json())["options"], **assertion)
        return await self.post("/auth/passkey/verify", {"credential": credential, "next": next_path},
                               headers={"User-Agent": user_agent})

    async def step_up(self, **assertion):
        options = await self.post("/api/user/passkeys/step-up/options")
        body = await options.get_json()
        assert body["required"] is True
        return await self.post("/api/user/passkeys/step-up/verify",
                               {"credential": self.authenticator.sign_in(body["options"], **assertion)})

    async def start_call(self):
        return await self.client.post("/api/voice/web-call", json=CALL)


@pytest.fixture(autouse=True)
def _transactions():
    with eval_transactions():
        yield


@pytest.fixture
async def world():
    w = World()
    await w.allow(ALEX)
    return w


async def _enrolled(world) -> str:
    """Alex signed in with Google on the Mac and added a passkey there; a clean browser follows. Returns the user_id."""
    await world.google_sign_in()
    assert (await world.enrol()).status_code == 201
    user_id = world.user_id()
    world.browser()
    return user_id


# ---------------------------------------------------------------------------------------------- the sign-in page

class TestSignInPage:
    async def test_login_serves_the_page_with_the_passkey_first_and_sets_no_cookie(self, world):
        resp = await world.client.get("/auth/login?next=/cabinet/call")
        html = await resp.get_data(as_text=True)
        assert resp.status_code == 200 and resp.headers["Content-Type"].startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-cache"
        assert html.index('id="passkey"') < html.index('id="google"')
        assert resp.headers.get_all("Set-Cookie") == []

    async def test_a_chosen_provider_skips_the_page(self, world):
        resp = await world.client.get("/auth/login?provider=google&next=/cabinet")
        assert resp.status_code == 302 and resp.headers["Location"].startswith("https://accounts.example/auth")

    async def test_the_google_path_redirects_with_state_next_and_invite_and_drops_an_off_site_next(self, world):
        resp = await world.client.get("/auth/login/google?next=/cabinet/call&code=INV-1")
        cookies = _values(resp)
        state = parse_qs(urlparse(resp.headers["Location"]).query)["state"][0]
        assert resp.status_code == 302 and cookies["oauth_state"] == state
        assert cookies["auth_next"] in ("/cabinet/call", "%2Fcabinet%2Fcall") and cookies["invite_code"] == "INV-1"
        evil = await world.client.get("/auth/login/google?next=https://evil.example/x")
        assert "evil.example" not in _values(evil).get("auth_next", "")


# ---------------------------------------------------------------------------------------------- passkey sign-in

class TestPasskeySignIn:
    async def test_a_passkey_signs_in_a_new_browser_and_returns_to_next(self, world):
        user_id = await _enrolled(world)
        world.kit.notices.sent.clear()
        resp = await world.passkey_sign_in()
        assert resp.status_code == 200 and await resp.get_json() == {"next": "/cabinet/call"}
        sessions = [s for s in world.kit.repo.sessions.values() if s.auth_kind is SessionAuthKind.PASSKEY]
        (session,) = sessions
        assert (session.user_id, session.label) == (user_id, "iPhone — Home Screen (call)")
        assert session.passkey_at == session.auth_at
        headers, cookies = _set_cookies(resp), _values(resp)
        assert world.kit.tokens.verify_device_access_token(cookies[ACCESS_COOKIE])["sid"] == session.sid
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
                assert flag in headers[name]
        assert _deleted(resp, SIGN_IN_COOKIE)
        assert (await world.client.get("/auth/me")).status_code == 200
        [(notified, message, params)] = world.kit.notices.sent
        assert (notified, message, params["platform_hint"]) == (
            user_id, UIMessage.DEVICE_PASSKEY_SIGN_IN_NOTICE, "iPhone — Home Screen")

    async def test_the_options_cookie_is_short_lived_strict_and_the_options_ask_for_verification(self, world):
        resp = await world.post("/auth/passkey/options")
        header = _set_cookies(resp)[SIGN_IN_COOKIE]
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=300"):
            assert flag in header
        options = (await resp.get_json())["options"]
        assert (options["rpId"], options["userVerification"], options["allowCredentials"]) == (RP, "required", [])

    async def test_an_off_site_next_returns_to_the_cabinet(self, world):
        await _enrolled(world)
        resp = await world.passkey_sign_in(next_path="https://evil.example/phish")
        assert await resp.get_json() == {"next": "/cabinet"}

    async def test_a_removed_passkey_is_refused_and_starts_no_session(self, world):
        await world.google_sign_in()
        row = (await (await world.enrol()).get_json())["passkey"]
        assert (await world.step_up()).status_code == 200
        assert (await world.post(f"/api/user/passkeys/{row['id']}/remove")).status_code == 200
        world.browser()
        before = set(world.kit.repo.sessions)
        resp = await world.passkey_sign_in()
        assert resp.status_code == 401 and await resp.get_json() == {"error": "passkey_refused"}
        assert set(world.kit.repo.sessions) == before and ACCESS_COOKIE not in _values(resp)

    async def test_a_replayed_challenge_is_refused(self, world):
        await _enrolled(world)
        options = await world.post("/auth/passkey/options")
        nonce = _values(options)[SIGN_IN_COOKIE]
        body = {"credential": world.authenticator.sign_in((await options.get_json())["options"]), "next": "/cabinet"}
        assert (await world.post("/auth/passkey/verify", body)).status_code == 200
        world.browser().set_cookie("localhost", SIGN_IN_COOKIE, nonce)     # the same nonce and response, again
        again = await world.post("/auth/passkey/verify", body)
        assert again.status_code == 401
        assert len([s for s in world.kit.repo.sessions.values() if s.auth_kind is SessionAuthKind.PASSKEY]) == 1

    async def test_an_inactive_user_is_refused_after_a_valid_assertion(self, world):
        user_id = await _enrolled(world)
        await world.db.collection(USERS).document(user_id).update({"is_active": False})
        resp = await world.passkey_sign_in()
        assert resp.status_code == 403 and await resp.get_json() == {"error": "access_denied"}
        assert not [s for s in world.kit.repo.sessions.values() if s.auth_kind is SessionAuthKind.PASSKEY]
        assert ACCESS_COOKIE not in _values(resp) and _deleted(resp, SIGN_IN_COOKIE)

    async def test_a_user_taken_off_the_whitelist_is_refused(self, world):
        await _enrolled(world)
        await world.allow("someone-else@example.com")
        resp = await world.passkey_sign_in()
        assert resp.status_code == 403
        assert not [s for s in world.kit.repo.sessions.values() if s.auth_kind is SessionAuthKind.PASSKEY]

    @pytest.mark.parametrize("cookie, body", [(False, {"credential": {"id": "x"}}), (True, {"credential": "x"}),
                                              (True, ["not", "an", "object"])],
                             ids=["no-nonce-cookie", "credential-not-an-object", "body-not-an-object"])
    async def test_a_verify_without_a_ceremony_is_400(self, world, cookie, body):
        if cookie:
            world.client.set_cookie("localhost", SIGN_IN_COOKIE, "nonce-x")
        resp = await world.client.post("/auth/passkey/verify", json=body)
        assert resp.status_code == 400 and world.kit.repo.sessions == {}


# ---------------------------------------------------------------------------------------------- enrolment and the list

class TestEnrolment:
    async def test_a_google_session_lists_no_passkey_and_is_offered_one(self, world):
        await world.google_sign_in()
        resp = await world.client.get("/api/user/passkeys")
        assert resp.status_code == 200 and await resp.get_json() == {"passkeys": [], "offer": True}

    async def test_adding_one_stores_it_under_the_session_label_and_tells_the_owner(self, world):
        await world.google_sign_in()
        world.kit.notices.sent.clear()
        resp = await world.enrol()
        assert resp.status_code == 201 and _deleted(resp, REGISTER_COOKIE)
        row = (await resp.get_json())["passkey"]
        assert (row["label"], row["platform_hint"], row["synced"], row["last_used_at"]) == (
            "Mac — Chrome", "Mac — Chrome", False, None)
        listed = await (await world.client.get("/api/user/passkeys")).get_json()
        assert listed["passkeys"] == [row]
        [(_, message, params)] = world.kit.notices.sent
        assert (message, params["label"]) == (UIMessage.PASSKEY_ADDED_NOTICE, "Mac — Chrome")

    async def test_a_passkey_session_is_not_offered_one(self, world):
        await _enrolled(world)
        await world.passkey_sign_in()
        listed = await (await world.client.get("/api/user/passkeys")).get_json()
        assert listed["offer"] is False and len(listed["passkeys"]) == 1
        assert listed["passkeys"][0]["last_used_at"] is not None

    async def test_a_response_that_does_not_verify_stores_nothing(self, world):
        await world.google_sign_in()
        resp = await world.enrol(user_verified=False)
        assert resp.status_code == 400 and await resp.get_json() == {"error": "passkey_refused"}
        assert world.kit.repo.passkeys == {} and _deleted(resp, REGISTER_COOKIE)

    async def test_a_verify_without_its_options_is_400(self, world):
        await world.google_sign_in()
        resp = await world.post("/api/user/passkeys/register/verify", {"credential": {"id": "x"}})
        assert resp.status_code == 400 and world.kit.repo.passkeys == {}

    async def test_an_inactive_user_cannot_add_one(self, world):
        await world.google_sign_in()
        options = await world.post("/api/user/passkeys/register/options")
        credential = world.authenticator.register((await options.get_json())["options"])
        await world.db.collection(USERS).document(world.user_id()).update({"is_active": False})
        assert (await world.post("/api/user/passkeys/register/options")).status_code == 403
        world.client.set_cookie("localhost", REGISTER_COOKIE, _values(options)[REGISTER_COOKIE])
        assert (await world.post("/api/user/passkeys/register/verify", {"credential": credential})).status_code == 403
        assert world.kit.repo.passkeys == {}

    async def test_a_session_ended_behind_the_gate_cache_cannot_add_one(self, world):
        await world.google_sign_in()
        assert (await world.client.get("/api/user/passkeys")).status_code == 200      # the gate caches it live
        await world.kit.repo.revoke(world.sid(), SessionRevokeReason.LOGOUT, world.kit.clock())          # another instance revoked it
        listed = await (await world.client.get("/api/user/passkeys")).get_json()
        assert listed["offer"] is False
        assert (await world.post("/api/user/passkeys/register/options")).status_code == 400
        world.client.set_cookie("localhost", REGISTER_COOKIE, "nonce-x")
        assert (await world.post("/api/user/passkeys/register/verify", {"credential": {"id": "x"}})).status_code == 400

    async def test_remove_revokes_one_and_another_users_or_unknown_is_404(self, world):
        await world.google_sign_in()
        row = (await (await world.enrol()).get_json())["passkey"]
        assert (await world.step_up()).status_code == 200
        assert (await world.post("/api/user/passkeys/unknown-key/remove")).status_code == 404
        resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
        assert resp.status_code == 200 and await resp.get_json() == {"removed": 1}
        assert (await (await world.client.get("/api/user/passkeys")).get_json())["passkeys"] == []
        assert (await world.post(f"/api/user/passkeys/{row['id']}/remove")).status_code == 404

    async def test_anonymous_calls_are_refused(self, world):
        for path in ("/api/user/passkeys/register/options", "/api/user/passkeys/step-up/options"):
            assert (await world.client.post(path, json={})).status_code == 401
        assert (await world.client.get("/api/user/passkeys")).status_code == 401


# ---------------------------------------------------------------------------------------------- the call-start passkey

class TestCallStart:
    async def test_a_user_without_a_passkey_starts_a_call_as_before(self, world):
        await world.google_sign_in()
        assert await (await world.post("/api/user/passkeys/step-up/options")).get_json() == {"required": False}
        assert (await world.start_call()).status_code == 201

    async def test_with_a_passkey_a_start_needs_a_fresh_assertion_and_holds_nothing_before_it(self, world):
        await world.google_sign_in()
        await world.enrol()
        resp = await world.start_call()
        assert resp.status_code == 403 and await resp.get_json() == {"error": "passkey_required"}
        assert world.calls.data == {}                                     # no marker, no ticket claimed

    async def test_a_step_up_lets_the_call_start_and_a_redial_within_five_minutes(self, world):
        await world.google_sign_in()
        await world.enrol()
        resp = await world.step_up()
        assert resp.status_code == 200 and _deleted(resp, STEP_UP_COOKIE)
        assert world.kit.repo.sessions[world.sid()].passkey_at is not None
        assert await (await world.post("/api/user/passkeys/step-up/options")).get_json() == {"required": False}
        assert (await world.start_call()).status_code == 201
        world.kit.clock.advance(minutes=4, seconds=59)
        world.calls.data.clear()                                          # the dropped call is gone
        assert (await world.start_call()).status_code == 201

    async def test_a_stale_assertion_asks_again(self, world):
        await world.google_sign_in()
        await world.enrol()
        await world.step_up()
        world.kit.clock.advance(minutes=5)
        assert (await world.start_call()).status_code == 403
        assert (await (await world.post("/api/user/passkeys/step-up/options")).get_json())["required"] is True

    async def test_a_passkey_sign_in_is_fresh_for_the_call_it_opened(self, world):
        await _enrolled(world)
        await world.passkey_sign_in()
        assert (await world.start_call()).status_code == 201

    async def test_a_refused_step_up_leaves_the_start_refused(self, world):
        await world.google_sign_in()
        await world.enrol()
        resp = await world.step_up(user_verified=False)
        assert resp.status_code == 400 and await resp.get_json() == {"error": "passkey_refused"}
        assert (await world.start_call()).status_code == 403

    async def test_a_step_up_verify_without_its_options_is_400(self, world):
        await world.google_sign_in()
        resp = await world.post("/api/user/passkeys/step-up/verify", {"credential": {"id": "x"}})
        assert resp.status_code == 400

    async def test_a_failed_check_refuses_the_start(self, world, monkeypatch, caplog):
        await world.google_sign_in()

        async def unreadable(user_id, sid):
            raise RuntimeError("firestore unavailable")

        monkeypatch.setattr(world.passkeys, "call_start_allowed", unreadable)
        resp = await world.start_call()
        assert resp.status_code == 503 and world.calls.data == {}
        assert "the passkey check failed" in caplog.text


# ---------------------------------------------------------------------------------------------- not configured

@pytest.mark.parametrize("method, path", [
    ("POST", "/auth/passkey/options"), ("POST", "/auth/passkey/verify"), ("GET", "/api/user/passkeys"),
    ("POST", "/api/user/passkeys/register/options"), ("POST", "/api/user/passkeys/register/verify"),
    ("POST", "/api/user/passkeys/key-1/remove"), ("POST", "/api/user/passkeys/step-up/options"),
    ("POST", "/api/user/passkeys/step-up/verify"),
])
async def test_without_the_passkey_service_every_passkey_route_is_501_and_a_call_starts(method, path):
    world = World(passkeys_wired=False)
    await world.allow(ALEX)
    await world.google_sign_in()
    resp = await world.client.open(path, method=method, json={})
    assert resp.status_code == 501
    assert (await world.start_call()).status_code == 201


# ---------------------------------------------------------------------------------------------- changing the set

class TestChangingPasskeysNeedsAFreshAssertion:
    """§7.3: with a passkey, removing or adding one needs a fresh assertion on the session: 400 `passkey_required`
    (never 401: authFetch would refresh and re-send a spent nonce)."""

    PASSKEY_REQUIRED = {"error": "passkey_required"}

    async def test_the_first_enrolment_is_free(self, world):
        await world.google_sign_in()
        assert (await world.enrol()).status_code == 201

    async def test_removing_is_400_passkey_required_without_a_fresh_assertion_and_removes_nothing(self, world):
        await world.google_sign_in()
        row = (await (await world.enrol()).get_json())["passkey"]
        resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
        assert resp.status_code == 400 and await resp.get_json() == self.PASSKEY_REQUIRED
        assert len((await (await world.client.get("/api/user/passkeys")).get_json())["passkeys"]) == 1

    async def test_the_bypass_remove_all_then_start_a_call_is_stopped_at_the_remove_step(self, world):
        await world.google_sign_in()
        row = (await (await world.enrol()).get_json())["passkey"]
        assert (await world.post(f"/api/user/passkeys/{row['id']}/remove")).status_code == 400
        resp = await world.start_call()
        assert resp.status_code == 403 and await resp.get_json() == {"error": "passkey_required"}

    async def test_removing_after_a_step_up_works(self, world):
        await world.google_sign_in()
        row = (await (await world.enrol()).get_json())["passkey"]
        assert (await world.step_up()).status_code == 200
        resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
        assert resp.status_code == 200 and await resp.get_json() == {"removed": 1}

    async def test_adding_a_second_is_400_passkey_required_at_options_and_at_verify(self, world):
        await world.google_sign_in()
        await world.enrol()
        refused = await world.post("/api/user/passkeys/register/options")
        assert refused.status_code == 400 and await refused.get_json() == self.PASSKEY_REQUIRED
        assert (await world.step_up()).status_code == 200
        options = await world.post("/api/user/passkeys/register/options")
        assert options.status_code == 200
        credential = world.authenticator.register((await options.get_json())["options"])
        world.kit.clock.advance(minutes=5)                                  # the assertion went stale meanwhile
        resp = await world.post("/api/user/passkeys/register/verify", {"credential": credential})
        assert resp.status_code == 400 and await resp.get_json() == self.PASSKEY_REQUIRED
        assert len((await (await world.client.get("/api/user/passkeys")).get_json())["passkeys"]) == 1

    async def test_adding_a_second_after_a_step_up_works(self, world):
        await world.google_sign_in()
        await world.enrol()
        await world.step_up()
        assert (await world.enrol()).status_code == 201


async def test_removing_a_passkey_after_the_session_was_revoked_elsewhere_is_400_and_removes_nothing(world):
    await world.google_sign_in()
    row = (await (await world.enrol()).get_json())["passkey"]
    await world.kit.repo.revoke(world.sid(), SessionRevokeReason.LOGOUT, world.kit.clock())          # another instance revoked it
    resp = await world.post(f"/api/user/passkeys/{row['id']}/remove")
    assert resp.status_code == 400 and await resp.get_json() == {"error": "Session ended"}
