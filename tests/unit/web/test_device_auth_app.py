"""The Cabinet devices routes (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6) over the real `DeviceSessionService`, the real
`SessionService` and the in-memory repository twin: the list with `is_current`, one row's logout, "log out all other
devices" (the current session stays), the kill switch, another user's sid refused, an anonymous call refused, a token
without `sid` marking no row, and a hostile label travelling as data (JSON), never as markup the server built."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import jwt
import pytest
from quart import Quart

from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import SessionAuthKind, SessionRevokeReason
from src.domain.user import UserProfile
from src.services.device_session_service import DeviceSessionService
from src.services.session_service import SessionService
from src.web.cabinet_auth import ACCESS_COOKIE, CabinetSessionVerifier
from src.web.device_auth_app import create_device_auth_blueprint
from tests.support.device_session_repo import FakeDeviceSessionRepository

SECRET = "test-secret-key-must-be-32-characters-minimum"
ALEX = UserProfile(user_id="user-alex", external_user_id="firebase|alex", email="alex@example.com",
                   display_name="Alex", account_id="acct-alex")
OTHER = UserProfile(user_id="user-other", external_user_id="firebase|other", email="other@example.com",
                    display_name="Other", account_id="acct-other")
ALEX_ACCOUNT = BillingAccount(account_id="acct-alex", tier=AccountTier.FAMILY, iam_policy={"user-alex": "owner"})
OTHER_ACCOUNT = BillingAccount(account_id="acct-other", tier=AccountTier.FAMILY, iam_policy={"user-other": "owner"})
PHONE, MAC = "iPhone — Home Screen (call)", "Mac — Chrome"
HOSTILE = '<img src=x onerror="alert(1)">'


class World:
    def __init__(self) -> None:
        self.repo = FakeDeviceSessionRepository()
        self.sessions = SessionService(secret_key=SECRET)
        notifications = AsyncMock()
        notifications.notify_ui_notice.return_value = True
        self.service = DeviceSessionService(self.repo, self.sessions, notifications)
        app = Quart(__name__)
        app.register_blueprint(create_device_auth_blueprint(CabinetSessionVerifier(self.sessions, self.service), self.service))
        self.client = app.test_client()

    async def sign_in(self, label, user=ALEX, account=ALEX_ACCOUNT, hint="iOS"):
        session, token, _ = await self.service.start(
            user, account, label=label, platform_hint=hint, auth_kind=SessionAuthKind.GOOGLE)
        return session, {"Authorization": f"Bearer {token}"}

    def sidless_token(self, user=ALEX, account=ALEX_ACCOUNT):
        """A validly signed, unexpired access JWT of the user without a `sid`: the shape the device gate refuses."""
        now = datetime.now(timezone.utc)
        claims = {"sub": user.user_id, "iat": int(now.timestamp()), "exp": int((now + timedelta(minutes=15)).timestamp()),
                  "account_id": account.account_id, "type": "access", "role": "owner"}
        return jwt.encode(claims, SECRET, algorithm="HS256")

    def live_sids(self, user_id):
        return {s.sid for s in self.repo.sessions.values() if s.user_id == user_id and s.revoked_at is None}


@pytest.fixture
def world():
    return World()


async def test_list_shows_live_sessions_and_marks_the_current_one(world):
    phone, phone_auth = await world.sign_in(PHONE)
    mac, _ = await world.sign_in(MAC, hint="macOS")
    await world.sign_in("Someone else", user=OTHER, account=OTHER_ACCOUNT)

    resp = await world.client.get("/api/user/devices", headers=phone_auth)

    assert resp.status_code == 200
    rows = {d["sid"]: d for d in (await resp.get_json())["devices"]}
    assert set(rows) == {phone.sid, mac.sid}
    assert rows[phone.sid]["label"] == PHONE and rows[phone.sid]["is_current"] is True
    assert rows[mac.sid]["label"] == MAC and rows[mac.sid]["is_current"] is False
    assert rows[mac.sid]["platform_hint"] == "macOS" and rows[mac.sid]["auth_kind"] == "google"
    assert rows[mac.sid]["created_at"] and rows[mac.sid]["last_seen_at"]


async def test_list_omits_revoked_sessions(world):
    phone, phone_auth = await world.sign_in(PHONE)
    mac, _ = await world.sign_in(MAC)
    await world.service.revoke(ALEX.user_id, mac.sid, SessionRevokeReason.LOGOUT)

    body = await (await world.client.get("/api/user/devices", headers=phone_auth)).get_json()

    assert [d["sid"] for d in body["devices"]] == [phone.sid]


async def test_a_token_without_sid_marks_no_row_current(world):
    # Spec change (RFC §7.1): the gate refuses a token without a live `sid` before the route runs, so no row can be
    # marked current by it, and in fact no row is listed at all.
    phone, _ = await world.sign_in(PHONE)

    resp = await world.client.get("/api/user/devices", headers={"Authorization": f"Bearer {world.sidless_token()}"})

    assert resp.status_code == 401
    assert "devices" not in (await resp.get_json())


async def test_the_cookie_token_works_like_the_bearer_header(world):
    phone, auth = await world.sign_in(PHONE)
    world.client.set_cookie("localhost", ACCESS_COOKIE, auth["Authorization"].split()[1])

    resp = await world.client.get("/api/user/devices")

    assert [d["is_current"] for d in (await resp.get_json())["devices"]] == [True]


async def test_a_hostile_label_is_returned_verbatim_as_json_data(world):
    _, auth = await world.sign_in(HOSTILE, hint=HOSTILE)

    resp = await world.client.get("/api/user/devices", headers=auth)

    assert resp.mimetype == "application/json"
    [row] = (await resp.get_json())["devices"]
    assert row["label"] == HOSTILE and row["platform_hint"] == HOSTILE


async def test_logout_of_one_row_revokes_it_and_keeps_the_rest(world):
    phone, phone_auth = await world.sign_in(PHONE)
    mac, _ = await world.sign_in(MAC)

    resp = await world.client.post(f"/api/user/devices/{mac.sid}/logout", headers=phone_auth)

    assert resp.status_code == 200
    assert await resp.get_json() == {"revoked": 1, "current": False}
    assert world.live_sids(ALEX.user_id) == {phone.sid}
    assert world.repo.sessions[mac.sid].revoke_reason is SessionRevokeReason.DEVICE_LIST


async def test_logout_of_the_current_row_says_so(world):
    phone, phone_auth = await world.sign_in(PHONE)

    resp = await world.client.post(f"/api/user/devices/{phone.sid}/logout", headers=phone_auth)

    assert await resp.get_json() == {"revoked": 1, "current": True}
    assert world.live_sids(ALEX.user_id) == set()


async def test_another_users_sid_is_refused_and_nothing_is_revoked(world):
    _, alex_auth = await world.sign_in(PHONE)
    theirs, _ = await world.sign_in("Their laptop", user=OTHER, account=OTHER_ACCOUNT)

    resp = await world.client.post(f"/api/user/devices/{theirs.sid}/logout", headers=alex_auth)

    assert resp.status_code == 404
    assert world.live_sids(OTHER.user_id) == {theirs.sid}


async def test_an_unknown_sid_is_refused(world):
    _, auth = await world.sign_in(PHONE)

    resp = await world.client.post("/api/user/devices/no-such-sid/logout", headers=auth)

    assert resp.status_code == 404


async def test_logout_others_keeps_the_current_session_and_other_users(world):
    phone, phone_auth = await world.sign_in(PHONE)
    await world.sign_in(MAC)
    await world.sign_in("iPad")
    theirs, _ = await world.sign_in("Their laptop", user=OTHER, account=OTHER_ACCOUNT)

    resp = await world.client.post("/api/user/devices/logout-others", headers=phone_auth)

    assert resp.status_code == 200
    assert await resp.get_json() == {"revoked": 2}
    assert world.live_sids(ALEX.user_id) == {phone.sid}
    assert world.live_sids(OTHER.user_id) == {theirs.sid}


async def test_logout_others_is_refused_when_the_token_names_no_session(world):
    # Spec change (RFC §7.1): the gate refuses a token without a live `sid` (401) before the route can answer 400.
    phone, _ = await world.sign_in(PHONE)

    resp = await world.client.post("/api/user/devices/logout-others",
                                   headers={"Authorization": f"Bearer {world.sidless_token()}"})

    assert resp.status_code == 401
    assert world.live_sids(ALEX.user_id) == {phone.sid}
    assert world.live_sids(ALEX.user_id) == {phone.sid}


async def test_logout_all_is_the_kill_switch_for_this_user_only(world):
    phone, phone_auth = await world.sign_in(PHONE)
    mac, _ = await world.sign_in(MAC)
    theirs, _ = await world.sign_in("Their laptop", user=OTHER, account=OTHER_ACCOUNT)

    resp = await world.client.post("/api/user/devices/logout-all", headers=phone_auth)

    assert await resp.get_json() == {"revoked": 2}
    assert world.live_sids(ALEX.user_id) == set()
    assert world.repo.sessions[phone.sid].revoke_reason is SessionRevokeReason.KILL_SWITCH
    assert world.live_sids(OTHER.user_id) == {theirs.sid}


@pytest.mark.parametrize("method, path", [
    ("get", "/api/user/devices"), ("post", "/api/user/devices/x/logout"),
    ("post", "/api/user/devices/logout-others"), ("post", "/api/user/devices/logout-all")])
async def test_an_anonymous_call_is_refused(world, method, path):
    await world.sign_in(PHONE)

    resp = await getattr(world.client, method)(path)

    assert resp.status_code == 401
    assert len(world.live_sids(ALEX.user_id)) == 1
