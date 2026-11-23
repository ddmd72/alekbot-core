"""The Cabinet's one session verifier, its gate and its cookies (`src/web/cabinet_auth.py`, CABINET_DEVICE_SESSIONS_RFC
§7.1 "The gate").

Real `SessionService`, real `DeviceSessionService` over the repository twin (`tests/support/cabinet_session_kit.py`);
the routes are a bare Quart app. Covers: a live session through the cookie or a Bearer header, every refusal (no
token, an empty Bearer, expired, forged, without a sid, without an account, a revoked session, a failed record read),
the sid of an expired cookie for refresh and logout, and the flags of the cookies written and deleted.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from quart import Quart, g, jsonify, make_response

from src.domain.device_session_policy import DeviceSessionPolicy
from src.web.cabinet_auth import (
    ACCESS_COOKIE, REFRESH_COOKIE, CabinetSessionVerifier, clear_session_cookies, make_auth_required,
    set_session_cookies,
)
from tests.support.cabinet_session_kit import KIT_SECRET, CabinetSessionKit


class World:
    def __init__(self) -> None:
        self.kit = CabinetSessionKit()
        app = Quart(__name__)
        verifier = self.kit.verifier

        @app.route("/who")
        @make_auth_required(verifier)
        async def who():
            return jsonify({"user": g.user_id, "account": g.account_id, "role": g.role, "sid": g.session_id})

        @app.route("/presented")
        async def presented():
            return jsonify({"presented": verifier.presented_session()})

        self.client = app.test_client()


@pytest.fixture
def world():
    return World()


def _token(**claims) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": "user-alex", "account_id": "acct-alex", "type": "access", "sid": "sid-x",
               "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=15)).timestamp()), **claims}
    return jwt.encode({k: v for k, v in payload.items() if v is not None}, KIT_SECRET, algorithm="HS256")


class TestTheGate:
    async def test_a_live_session_cookie_sets_the_identity(self, world):
        signed = await world.kit.sign_in(role="owner")
        world.kit.set_cookies(world.client, signed, refresh=False)
        resp = await world.client.get("/who")
        assert resp.status_code == 200
        assert await resp.get_json() == {"user": "user-alex", "account": "acct-alex", "role": "owner",
                                         "sid": signed.sid}

    async def test_a_bearer_header_goes_through_the_same_check_and_wins_over_the_cookie(self, world):
        signed = await world.kit.sign_in()
        other = await world.kit.sign_in("user-robin", "acct-robin")
        world.kit.set_cookies(world.client, other, refresh=False)
        resp = await world.client.get("/who", headers=signed.bearer)
        assert (await resp.get_json())["user"] == "user-alex"
        await world.kit.revoke(signed)
        assert (await world.client.get("/who", headers=signed.bearer)).status_code == 401

    async def test_no_token_or_an_empty_bearer_is_missing_authorization(self, world):
        for headers in ({}, {"Authorization": "Bearer "}):
            resp = await world.client.get("/who", headers=headers)
            assert resp.status_code == 401
            assert await resp.get_json() == {"error": "Missing authorization"}

    async def test_a_revoked_session_is_refused_at_once_on_this_instance(self, world):
        signed = await world.kit.sign_in()
        world.kit.set_cookies(world.client, signed, refresh=False)
        assert (await world.client.get("/who")).status_code == 200
        await world.kit.revoke(signed)
        resp = await world.client.get("/who")
        assert resp.status_code == 401
        assert await resp.get_json() == {"error": "Invalid or expired token"}

    async def test_a_session_past_its_idle_end_is_refused(self, world):
        signed = await world.kit.sign_in()
        world.kit.clock.advance(days=14, seconds=1)
        assert (await world.client.get("/who", headers=signed.bearer)).status_code == 401

    @pytest.mark.parametrize("token", [
        pytest.param(_token(sid=None), id="no sid: a token minted before device sessions"),
        pytest.param(_token(account_id=None), id="no account"),
        pytest.param(_token(exp=int((datetime.now(timezone.utc) - timedelta(seconds=1)).timestamp())), id="expired"),
        pytest.param(jwt.encode({"sub": "user-alex", "account_id": "acct-alex", "type": "access", "sid": "sid-x",
                                 "exp": 4102444800}, "another-secret-0123456789abcdef-xyz", algorithm="HS256"),
                     id="forged"),
        pytest.param(_token(type="refresh"), id="another type"),
    ])
    async def test_a_token_that_does_not_verify_is_refused_before_any_record_read(self, world, token):
        reads = []
        world.kit.repo.get = lambda sid: reads.append(sid)    # type: ignore[method-assign]
        resp = await world.client.get("/who", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401 and reads == []

    async def test_an_unknown_sid_is_refused(self, world):
        assert (await world.client.get("/who", headers={"Authorization": f"Bearer {_token()}"})).status_code == 401

    async def test_a_failed_record_read_refuses(self, world):
        signed = await world.kit.sign_in()
        world.kit.clock.advance(seconds=DeviceSessionPolicy.GATE_CACHE_TTL.total_seconds() + 1)   # past the cache

        async def outage(sid):
            raise RuntimeError("firestore unavailable")
        world.kit.repo.get = outage    # type: ignore[method-assign]
        assert (await world.client.get("/who", headers=signed.bearer)).status_code == 401


class TestPresentedSession:
    async def test_an_expired_access_cookie_still_names_its_user_and_sid(self, world):
        world.kit.clock.set(datetime.now(timezone.utc) - timedelta(hours=1))
        signed = await world.kit.sign_in()
        world.kit.set_cookies(world.client, signed, refresh=False)
        assert (await (await world.client.get("/presented")).get_json()) == {"presented": ["user-alex", signed.sid]}

    async def test_a_bearer_header_is_not_a_presented_session(self, world):
        signed = await world.kit.sign_in()
        resp = await world.client.get("/presented", headers=signed.bearer)
        assert await resp.get_json() == {"presented": None}

    @pytest.mark.parametrize("cookie", [
        pytest.param("not-a-jwt", id="malformed"),
        pytest.param(_token(sid=None), id="no sid"),
        pytest.param(_token(sub=""), id="no user"),
    ])
    async def test_a_cookie_that_does_not_name_a_session_is_none(self, world, cookie):
        world.client.set_cookie("localhost", ACCESS_COOKIE, cookie)
        assert await (await world.client.get("/presented")).get_json() == {"presented": None}

    def test_access_expiry_is_the_tokens_exp(self):
        assert CabinetSessionVerifier.access_expiry(_token(exp=1_900_000_000)) == 1_900_000_000


class TestCookies:
    async def _set_cookie_headers(self, write) -> dict:
        app = Quart(__name__)

        @app.route("/c")
        async def c():
            response = await make_response("ok")
            await write(response)
            return response

        resp = await app.test_client().get("/c")
        return {h.split("=", 1)[0]: h for h in resp.headers.get_all("Set-Cookie")}

    async def test_both_cookies_are_host_cookies_living_to_the_sessions_absolute_end(self):
        kit = CabinetSessionKit()
        signed = await kit.sign_in()

        async def write(response):
            set_session_cookies(response, signed.session, signed.access_token, signed.refresh_secret)
        headers = await self._set_cookie_headers(write)
        thirty_days = int(DeviceSessionPolicy.ABSOLUTE_TTL.total_seconds())
        for name, value in ((ACCESS_COOKIE, signed.access_token), (REFRESH_COOKIE, signed.refresh_secret)):
            header = headers[name]
            assert header.startswith(f"{name}={value};")
            for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={thirty_days}"):
                assert flag in header
            assert "Domain" not in header

    async def test_a_grace_refresh_writes_the_access_cookie_only(self):
        kit = CabinetSessionKit()
        signed = await kit.sign_in()

        async def write(response):
            set_session_cookies(response, signed.session, signed.access_token, None)
        assert set(await self._set_cookie_headers(write)) == {ACCESS_COOKIE}

    async def test_clearing_deletes_both_with_the_host_flags(self):
        async def write(response):
            clear_session_cookies(response)
        headers = await self._set_cookie_headers(write)
        for name in (ACCESS_COOKIE, REFRESH_COOKIE):
            assert headers[name].startswith(f"{name}=;")
            for flag in ("Secure", "Path=/", "Max-Age=0"):
                assert flag in headers[name]
