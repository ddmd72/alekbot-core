"""The Cabinet's facts PIN routes and the guard on the facts routes (CABINET_DEVICE_SESSIONS_RFC §7.7), over the real
`FactsPinService`, `DeviceSessionService`, `AccountLockService` and `UserNotificationService` of `PinLockWorld` (the
in-memory twins of the PIN and device stores) behind the real Cabinet gate. Codes in this file are `0001` (the PIN),
`0002` (the duress code) and `0000` (a wrong one) only, a change of codes swapping the two; no response and no log line may carry one."""
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.domain.billing import AccountTier, BillingAccount
from src.domain.device_session import SessionAuthKind
from src.domain.facts_pin_policy import PinSurface, PinVerdict
from src.domain.ui_messages import UIMessage
from src.domain.user import UserProfile
from src.services.session_service import SessionService
from src.web.cabinet_auth import CabinetSessionVerifier
from src.web.facts_pin_app import GUARD_MARK, create_facts_pin_blueprint
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_store import FactStore
from tests.support.pin_lock_world import DURESS, PIN, SECRET, WRONG, PinLockWorld, en

ALEX = UserProfile(user_id="user-alex", external_user_id="firebase|alex", email="alex@example.com",
                   display_name="Alex", account_id="acct-alex")
ACCOUNT = BillingAccount(account_id="acct-alex", tier=AccountTier.FAMILY, iam_policy={"user-alex": "owner"})
CODES = (PIN, DURESS, WRONG)
FACTS_GETS = ["/api/user/facts", "/api/user/fact-domains", "/api/user/facts/browse",
              "/api/user/facts/recently-closed", "/api/user/facts/review-queue"]


class Site:
    """One Quart app with the PIN blueprint and the Cabinet blueprint over one `PinLockWorld`."""

    def __init__(self) -> None:
        self.world = PinLockWorld()
        self.world.clock.now = datetime.now(timezone.utc)    # the gate verifies a JWT's `exp` against the real time
        self.facts = FactStore()
        verifier = CabinetSessionVerifier(SessionService(secret_key=SECRET), self.world.device_sessions)
        self.app = Quart("facts_pin")
        self.app.register_blueprint(create_facts_pin_blueprint(
            verifier, self.world.service, self.world.device_sessions))
        self.app.register_blueprint(create_user_cabinet_blueprint(
            invite_service=MagicMock(), cabinet_sessions=verifier, user_repo=MagicMock(),
            fact_repo=self.facts.repo, embedding_service=self.facts.embedder, facts_pin=self.world.service))
        self.client = self.app.test_client()
        self.bodies = []

    async def sign_in(self, sid: str = "sid-alex", **kwargs) -> dict:
        session = (await self.world.add_session(sid, **kwargs))
        token = self.world.device_sessions.mint_access(session, ALEX, ACCOUNT)
        return {"Authorization": f"Bearer {token}"}

    async def call(self, auth: dict, method: str, path: str, body=None):
        response = await self.client.open(path, method=method, headers=auth, json=body)
        self.bodies.append(await response.get_data(as_text=True))
        return response


@pytest.fixture
async def site():
    s = Site()
    await s.world.seed()
    return s


@pytest.fixture
async def alex(site):
    return await site.sign_in("sid-1")


def codes(pin=PIN, duress=DURESS):
    return {"pin": pin, "pin_confirm": pin, "duress": duress, "duress_confirm": duress}


async def pinned(site, alex):
    await site.world.set_pin()
    return alex


# ---------------------------------------------------------------- status

async def test_status_for_a_user_without_a_pin(site, alex):
    body = await (await site.call(alex, "GET", "/api/user/facts-pin")).get_json()
    assert body == {"has_pin": False, "unlocked": False, "reset_due_at": None, "can_request_reset": False}


async def test_status_with_a_pin_locked_then_unlocked(site, alex):
    await site.world.set_pin()
    body = await (await site.call(alex, "GET", "/api/user/facts-pin")).get_json()
    assert body["has_pin"] is True and body["unlocked"] is False and body["can_request_reset"] is True
    await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert (await (await site.call(alex, "GET", "/api/user/facts-pin")).get_json())["unlocked"] is True


async def test_status_says_a_passkey_session_cannot_ask_for_a_reset(site):
    await site.world.set_pin()
    passkey = await site.sign_in("sid-pk", auth_kind=SessionAuthKind.PASSKEY)
    body = await (await site.call(passkey, "GET", "/api/user/facts-pin")).get_json()
    assert body["can_request_reset"] is False


async def test_status_shows_the_pending_reset_and_a_due_one_reads_as_no_pin(site, alex):
    await site.world.set_pin()
    due = (await (await site.call(alex, "POST", "/api/user/facts-pin/reset")).get_json())["reset_due_at"]
    status = await (await site.call(alex, "GET", "/api/user/facts-pin")).get_json()
    assert status["reset_due_at"] == due and status["has_pin"] is True
    site.world.clock.advance(hours=25)
    assert (await (await site.call(alex, "GET", "/api/user/facts-pin")).get_json())["has_pin"] is False


# ---------------------------------------------------------------- set

async def test_set_stores_a_pin_and_refuses_a_second_first_pin(site, alex):
    first = await site.call(alex, "POST", "/api/user/facts-pin/set", codes())
    assert first.status_code == 200 and (await first.get_json()) == {"status": "done"}
    again = await site.call(alex, "POST", "/api/user/facts-pin/set", codes("0002", "0001"))
    assert again.status_code == 409 and (await again.get_json())["error"] == "already_set"


@pytest.mark.parametrize("body,error", [
    ({**codes(), "pin_confirm": "0002"}, "mismatch"),
    ({**codes(), "duress_confirm": PIN}, "mismatch"),
    (codes("1111", DURESS), "trivial"),
    (codes(PIN, PIN), "duress_same_as_pin"),
    (codes("40", DURESS), "not_four_digits"),
    ({"pin": PIN}, "invalid_body"),
    ({**codes(), "pin": 1}, "invalid_body"),
    ([PIN], "invalid_body"),
])
async def test_set_refuses_a_bad_body_with_400_and_stores_nothing(site, alex, body, error):
    response = await site.call(alex, "POST", "/api/user/facts-pin/set", body)
    assert response.status_code == 400 and (await response.get_json())["error"] == error
    assert site.world.pins.records == {}


# ---------------------------------------------------------------- change

async def test_change_replaces_the_codes_when_the_current_pin_is_right(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": PIN, **codes("0002", "0001")})
    assert response.status_code == 200
    now_pin = await site.world.service.check("user-alex", "acct-alex", "0002", PinSurface.CABINET)
    assert now_pin.verdict is PinVerdict.OK


async def test_change_with_a_wrong_current_reports_the_attempts_left(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": WRONG, **codes("0002", "0001")})
    assert response.status_code == 400
    assert await response.get_json() == {"error": "wrong_code", "attempts_left": 4}


async def test_change_with_the_duress_code_locks_the_account(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": DURESS, **codes("0002", "0001")})
    assert response.status_code == 423 and (await response.get_json()) == {"error": "account_locked"}
    assert site.world.flag()[0] is False


async def test_change_refuses_new_codes_that_break_a_rule_before_counting_the_current(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": WRONG, **codes("1234", DURESS)})
    assert response.status_code == 400 and (await response.get_json())["error"] == "trivial"
    assert site.world.pins.records["user-alex"].wrong_in_row == 0


async def test_change_without_a_pin_is_409_and_a_mismatch_is_400(site, alex):
    none = await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": PIN, **codes("0002", "0001")})
    assert none.status_code == 409 and (await none.get_json())["error"] == "no_pin"
    mismatch = await site.call(alex, "POST", "/api/user/facts-pin/change",
                               {"current": PIN, **codes(), "pin_confirm": "0002"})
    assert mismatch.status_code == 400 and (await mismatch.get_json())["error"] == "mismatch"
    missing = await site.call(alex, "POST", "/api/user/facts-pin/change", codes())
    assert (await missing.get_json())["error"] == "invalid_body"


# ---------------------------------------------------------------- remove

async def test_remove_needs_the_current_pin_and_tells_the_owner(site, alex):
    await site.world.set_pin()
    wrong = await site.call(alex, "POST", "/api/user/facts-pin/remove", {"current": WRONG})
    assert wrong.status_code == 400 and (await wrong.get_json())["attempts_left"] == 4
    assert "user-alex" in site.world.pins.records
    done = await site.call(alex, "POST", "/api/user/facts-pin/remove", {"current": PIN})
    assert done.status_code == 200 and "user-alex" not in site.world.pins.records
    stamp = site.world.clock.now.strftime("%Y-%m-%d %H:%M UTC")
    assert en(UIMessage.FACTS_PIN_REMOVED_NOTICE, time=stamp) in [text for _, _, text in site.world.sent]


async def test_remove_without_a_pin_is_409_and_a_bad_body_is_400(site, alex):
    assert (await site.call(alex, "POST", "/api/user/facts-pin/remove", {"current": PIN})).status_code == 409
    bad = await site.call(alex, "POST", "/api/user/facts-pin/remove", {"current": 1})
    assert bad.status_code == 400 and (await bad.get_json())["error"] == "invalid_body"


# ---------------------------------------------------------------- unlock and relock

async def test_unlock_opens_the_session_for_fifteen_minutes_and_relock_closes_it(site, alex):
    await site.world.set_pin()
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 403
    response = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert response.status_code == 200 and (await response.get_json()) == {"status": "unlocked"}
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 200
    site.world.clock.advance(minutes=16)
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 403
    await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    relock = await site.call(alex, "POST", "/api/user/facts-pin/relock")
    assert relock.status_code == 204
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 403


async def test_unlock_by_one_session_does_not_open_another(site, alex):
    await site.world.set_pin()
    other_device = await site.sign_in("sid-2")
    await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert (await site.call(other_device, "GET", "/api/user/facts")).status_code == 403


async def test_a_wrong_pin_counts_down_and_the_fifth_locks_the_account(site, alex):
    await site.world.set_pin()
    left = []
    for _ in range(4):
        response = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": WRONG})
        assert response.status_code == 400
        left.append((await response.get_json())["attempts_left"])
    assert left == [4, 3, 2, 1]
    fifth = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": WRONG})
    assert fifth.status_code == 423 and (await fifth.get_json()) == {"error": "account_locked"}
    assert site.world.flag()[0] is False


async def test_the_duress_code_answers_like_a_lock(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": DURESS})
    assert response.status_code == 423 and (await response.get_json()) == {"error": "account_locked"}
    assert site.world.flag()[0] is False
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 401    # every session is gone


async def test_unlock_without_a_pin_or_with_a_bad_body_is_refused(site, alex):
    assert (await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})).status_code == 409
    bad = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {})
    assert bad.status_code == 400 and (await bad.get_json())["error"] == "invalid_body"


async def test_unlock_of_a_session_that_ended_meanwhile_is_403(site, alex, monkeypatch):
    await site.world.set_pin()

    async def ended(*args):
        raise PermissionError("gone")

    monkeypatch.setattr(site.world.service, "unlock_session", ended)
    response = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert response.status_code == 403 and (await response.get_json())["error"] == "session_ended"


async def test_unlock_that_could_not_open_the_session_is_403(site, alex, monkeypatch):
    await site.world.set_pin()
    real = site.world.service.unlock_session

    async def not_opened(*args):
        outcome, _ = await real(*args)
        return outcome, False

    monkeypatch.setattr(site.world.service, "unlock_session", not_opened)
    response = await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert response.status_code == 403 and (await response.get_json())["error"] == "session_ended"


# ---------------------------------------------------------------- reset

async def test_reset_waits_twenty_four_hours_and_can_be_cancelled(site, alex):
    await site.world.set_pin()
    response = await site.call(alex, "POST", "/api/user/facts-pin/reset")
    body = await response.get_json()
    assert response.status_code == 200 and body["status"] == "pending"
    assert body["reset_due_at"] == (site.world.clock.now + timedelta(hours=24)).isoformat()
    assert (await site.call(alex, "POST", "/api/user/facts-pin/reset/cancel")).status_code == 200
    again = await site.call(alex, "POST", "/api/user/facts-pin/reset/cancel")
    assert again.status_code == 409 and (await again.get_json())["error"] == "no_reset_pending"


async def test_a_passkey_session_cannot_ask_for_a_reset(site):
    await site.world.set_pin()
    passkey = await site.sign_in("sid-pk", auth_kind=SessionAuthKind.PASSKEY)
    response = await site.call(passkey, "POST", "/api/user/facts-pin/reset")
    assert response.status_code == 403 and (await response.get_json())["error"] == "reset_refused"


async def test_a_reset_without_a_pin_is_refused_and_a_due_one_removes_the_pin(site, alex):
    assert (await site.call(alex, "POST", "/api/user/facts-pin/reset")).status_code == 403
    await site.world.set_pin()
    await site.call(alex, "POST", "/api/user/facts-pin/reset")
    site.world.clock.advance(hours=25)
    assert (await site.call(alex, "GET", "/api/user/facts")).status_code == 200    # no PIN again: the tab opens
    assert "user-alex" not in site.world.pins.records
    stamp = site.world.clock.now.strftime("%Y-%m-%d %H:%M UTC")
    assert en(UIMessage.FACTS_PIN_RESET_DONE_NOTICE, time=stamp) in [text for _, _, text in site.world.sent]


async def test_the_reset_of_a_session_that_is_gone_is_refused(site, alex, monkeypatch):
    await site.world.set_pin()

    async def none(user_id, sid):
        return None

    monkeypatch.setattr(site.world.device_sessions, "current", none)
    assert (await site.call(alex, "POST", "/api/user/facts-pin/reset")).status_code == 403


# ---------------------------------------------------------------- relock of a session that is not the caller's

async def test_relock_is_204_even_when_nothing_was_unlocked(site, alex):
    assert (await site.call(alex, "POST", "/api/user/facts-pin/relock")).status_code == 204


# ---------------------------------------------------------------- the gate

@pytest.mark.parametrize("method,path", [("GET", "/api/user/facts-pin"), ("POST", "/api/user/facts-pin/set"),
                                         ("POST", "/api/user/facts-pin/unlock"), ("POST", "/api/user/facts-pin/relock"),
                                         ("POST", "/api/user/facts-pin/reset")])
async def test_an_anonymous_call_is_401(site, method, path):
    response = await site.call({}, method, path, {} if method == "POST" else None)
    assert response.status_code == 401


# ---------------------------------------------------------------- the facts routes

def _facts_rules(app):
    return [rule for rule in app.url_map.iter_rules()
            if rule.rule.startswith(("/api/user/facts", "/api/user/fact-")) and not rule.rule.startswith(
                "/api/user/facts-pin")]


def test_every_facts_route_carries_the_guard(site):
    rules = _facts_rules(site.app)
    assert sorted(rule.rule for rule in rules) == sorted([
        "/api/user/facts", "/api/user/fact-domains", "/api/user/facts/browse", "/api/user/facts/recently-closed",
        "/api/user/facts/review-queue", "/api/user/facts/review-answers", "/api/user/facts/search",
        "/api/user/facts/<fact_id>/invalidate"])
    unguarded = [rule.rule for rule in rules if not getattr(site.app.view_functions[rule.endpoint], GUARD_MARK, False)]
    assert unguarded == [], f"a facts route without @facts_unlock_required: {unguarded}"


async def _hit(site, auth, method, path):
    body = {"query": "x"} if path.endswith("/search") else {"answers": []} if "review-answers" in path else None
    return await site.call(auth, method, path, body)


ALL_FACTS = [("GET", p) for p in FACTS_GETS] + [
    ("POST", "/api/user/facts/review-answers"), ("POST", "/api/user/facts/search"),
    ("POST", "/api/user/facts/f-1/invalidate")]


@pytest.mark.parametrize("method,path", ALL_FACTS)
async def test_a_facts_route_is_locked_when_a_pin_is_set_and_the_session_is_not_unlocked(site, alex, method, path):
    await site.world.set_pin()
    response = await _hit(site, alex, method, path)
    assert response.status_code == 403 and (await response.get_json()) == {"error": "facts_locked"}


@pytest.mark.parametrize("method,path", ALL_FACTS)
async def test_a_facts_route_answers_when_the_session_is_unlocked(site, alex, method, path):
    await site.world.set_pin()
    await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
    assert (await _hit(site, alex, method, path)).status_code != 403


@pytest.mark.parametrize("method,path", ALL_FACTS)
async def test_a_facts_route_answers_when_the_user_has_no_pin(site, alex, method, path):
    response = await _hit(site, alex, method, path)
    assert response.status_code not in (401, 403)


async def _counted(site):
    from src.domain.fact_domains import Domain
    from tests.support.fact_kit import make_fact
    await site.facts.repo.add_fact(make_fact("morning yoga 30 min", account=ALEX.account_id,
                                             primary_domain=Domain.SPORT, domain_keys=[Domain.SPORT]))


async def test_the_domain_counts_are_locked_like_the_facts_and_nothing_is_counted(site, alex):
    # CABINET_REDESIGN_RFC §5.2: counts are facts metadata, so a locked memory does not show them either.
    from tests.support.fact_store import FACTS
    await _counted(site)
    await site.world.set_pin()

    response = await site.call(alex, "GET", "/api/user/facts/domain-counts?state=current")

    assert response.status_code == 403 and (await response.get_json()) == {"error": "facts_locked"}
    assert site.facts.db.collection(FACTS).executed == []


async def test_the_domain_counts_answer_once_unlocked_or_without_a_pin(site, alex):
    from src.domain.fact_domains import Domain
    await _counted(site)
    open_body = await (await site.call(alex, "GET", "/api/user/facts/domain-counts")).get_json()
    await site.world.set_pin()
    await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})

    unlocked = await site.call(alex, "GET", "/api/user/facts/domain-counts")

    assert unlocked.status_code == 200
    assert open_body == await unlocked.get_json() == {"total": 1, "counts": {Domain.SPORT: 1}}


async def test_the_guard_fails_closed_when_the_pin_read_fails(site, alex, monkeypatch):
    async def broken(*args):
        raise RuntimeError("store down")

    monkeypatch.setattr(site.world.service, "facts_open", broken)
    response = await site.call(alex, "GET", "/api/user/facts")
    assert response.status_code == 403 and (await response.get_json())["error"] == "facts_locked"


async def test_without_a_pin_service_the_facts_routes_are_open():
    from src.web.facts_pin_app import make_facts_unlock_required
    guard = make_facts_unlock_required(None)

    async def route():
        return "ok"

    assert await guard(route)() == "ok" and getattr(guard(route), GUARD_MARK) is True


# ---------------------------------------------------------------- no code in any answer or log

async def test_no_code_appears_in_any_response_or_log_line(site, alex, caplog):
    caplog.set_level(logging.DEBUG)
    handler_records = []

    class Capture(logging.Handler):
        def emit(self, record):
            handler_records.append(record.getMessage())

    from src.utils.logger import logger
    capture = Capture()
    logger.addHandler(capture)
    try:
        await site.call(alex, "POST", "/api/user/facts-pin/set", codes())
        await site.call(alex, "POST", "/api/user/facts-pin/set", codes(PIN, PIN))
        await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": WRONG})
        await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": PIN})
        await site.call(alex, "POST", "/api/user/facts-pin/change", {"current": WRONG, **codes("0002", "0001")})
        await site.call(alex, "POST", "/api/user/facts-pin/remove", {"current": WRONG})
        await site.call(alex, "GET", "/api/user/facts-pin")
        await site.call(alex, "POST", "/api/user/facts-pin/unlock", {"pin": DURESS})
    finally:
        logger.removeHandler(capture)
    text = "\n".join(site.bodies + handler_records + [r.getMessage() for r in caplog.records])
    for code in CODES:
        assert code not in text, code
