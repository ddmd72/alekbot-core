"""FactsPinService (CABINET_DEVICE_SESSIONS_RFC §7.7–§7.9) in `tests/support/pin_lock_world.py`: the PIN is opt-in
(no record: nothing checked, counted or locked), codes are keyed digests and never logged, an attempt counts across
surfaces and the duress code or the fifth wrong code locks the whole account (flag, sessions, passkeys, pending reset,
MCP grants, a notice on every channel), a change or removal needs the current PIN, the session unlock lasts 15 minutes,
and a forgotten PIN is reset 24 h after a fresh Google sign-in asks, unless cancelled."""
from datetime import timedelta

import pytest

from src.domain.device_session import SessionAuthKind, SessionRevokeReason
from src.domain.facts_pin_policy import (
    CodeProblem, FactsPinPolicy as P, PinChange, PinChangeStatus, PinOutcome, PinSurface, PinVerdict,
)
from src.domain.secret_digest import keyed_digest
from src.domain.ui_messages import UIMessage
from src.services.facts_pin_service import FactsPinService
from tests.support.pin_lock_world import DURESS, KEY, MAC, PIN, T0, WRONG, PinLockWorld, en

CHAT, CABINET = PinSurface.CHAT, PinSurface.CABINET
DONE = PinChange(PinChangeStatus.DONE)


@pytest.fixture
async def world():
    w = PinLockWorld()
    await w.seed()
    return w


async def check(world, code, surface=CHAT, user_id="user-alex"):
    return await world.service.check(user_id, f"acct-{user_id.removeprefix('user-')}", code, surface)


# ---------------------------------------------------------------------------------------------- opt-in

class TestWithoutAPin:
    async def test_nothing_is_counted_or_locked_and_the_facts_are_open(self, world):
        for _ in range(7):
            assert await check(world, WRONG) == PinOutcome(PinVerdict.NO_PIN)
        assert await check(world, DURESS) == PinOutcome(PinVerdict.NO_PIN)
        assert world.pins.records == {} and world.flag() == (True, None)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is True
        assert world.devices.sessions["sid-alex"].revoked_at is None and world.sent == []

    async def test_a_change_or_removal_has_nothing_to_change(self, world):
        assert await world.service.change("user-alex", "acct-alex", PIN, "0002", "0001") == PinChange(
            PinChangeStatus.NO_PIN)
        assert await world.service.remove("user-alex", "acct-alex", PIN) == PinChange(PinChangeStatus.NO_PIN)
        assert world.pins.records == {}


class TestSet:
    async def test_the_codes_are_stored_as_keyed_digests_of_the_user_and_the_code(self, world):
        assert await world.service.set("user-alex", PIN, DURESS) == DONE
        record = world.pins.records["user-alex"]
        assert record.pin_digest == keyed_digest(KEY, "facts_pin", f"user-alex:{PIN}")
        assert record.duress_digest == keyed_digest(KEY, "duress_pin", f"user-alex:{DURESS}")
        assert (record.wrong_in_row, record.updated_at) == (0, T0)

    @pytest.mark.parametrize("pin, duress, problem", [
        ("1234", DURESS, CodeProblem.TRIVIAL), (PIN, "0000", CodeProblem.TRIVIAL),
        ("000", DURESS, CodeProblem.NOT_FOUR_DIGITS), (PIN, PIN, CodeProblem.DURESS_SAME_AS_PIN),
    ])
    async def test_refused_codes_store_nothing(self, world, pin, duress, problem):
        assert await world.service.set("user-alex", pin, duress) == PinChange(PinChangeStatus.CODES_REFUSED,
                                                                              problem=problem)
        assert world.pins.records == {}

    async def test_a_second_set_is_refused_and_keeps_the_first(self, world):
        await world.set_pin()
        before = world.pins.records["user-alex"]
        assert await world.service.set("user-alex", "0002", "0001") == PinChange(PinChangeStatus.ALREADY_SET)
        assert world.pins.records["user-alex"] == before


# ---------------------------------------------------------------------------------------------- attempts

class TestCheck:
    async def test_the_pin_answers_ok_on_either_surface_and_clears_the_counter(self, world):
        await world.set_pin()
        assert await check(world, WRONG, CABINET) == PinOutcome(PinVerdict.WRONG, attempts_left=4)
        assert await check(world, WRONG, CHAT) == PinOutcome(PinVerdict.WRONG, attempts_left=3)
        assert await check(world, PIN, CHAT) == PinOutcome(PinVerdict.OK)
        assert world.pins.records["user-alex"].wrong_in_row == 0 and world.flag() == (True, None)

    async def test_another_users_pin_is_a_wrong_code(self, world):
        await world.set_pin("user-other")
        await world.set_pin()
        world.pins.records["user-other"] = world.pins.records["user-other"].model_copy(
            update={"pin_digest": keyed_digest(KEY, "facts_pin", f"user-other:{WRONG}")})
        assert await check(world, WRONG) == PinOutcome(PinVerdict.WRONG, attempts_left=4)

    async def test_the_fifth_wrong_code_locks_the_whole_account(self, world):
        await world.set_pin()
        await world.service.request_reset(await world.add_session("sid-fresh"))
        world.channels.channels.clear()
        for left in (4, 3, 2, 1):
            assert await check(world, WRONG) == PinOutcome(PinVerdict.WRONG, attempts_left=left)
        assert await check(world, WRONG) == PinOutcome(PinVerdict.LOCKED, locked_now=True)
        assert world.flag() == (False, "pin_attempts")
        self.assert_locked_everywhere(world, en(UIMessage.ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE))

    async def test_the_duress_code_locks_the_whole_account_at_once(self, world):
        await world.set_pin()
        await world.service.request_reset(await world.add_session("sid-fresh"))
        world.channels.channels.clear()
        assert await check(world, DURESS, CHAT) == PinOutcome(PinVerdict.DURESS, locked_now=True)
        assert world.flag() == (False, "duress")
        self.assert_locked_everywhere(world, en(UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE))

    @staticmethod
    def assert_locked_everywhere(world, notice):
        sessions = world.devices.sessions
        assert {sid: s.revoke_reason for sid, s in sessions.items() if s.user_id == "user-alex"} == {
            "sid-alex": SessionRevokeReason.ACCOUNT_LOCK, "sid-fresh": SessionRevokeReason.ACCOUNT_LOCK}
        assert sessions["sid-other"].revoked_at is None
        assert world.devices.passkeys["key-alex"].revoked_at == T0
        assert world.pins.records["user-alex"].reset_requested_at is None
        assert world.token_revoked("user-alex") and not world.token_revoked("user-other")
        assert world.sent == [("slack", "C-alex", notice), ("telegram", "chat-alex", notice)]
        assert world.flag("user-other") == (True, None)

    async def test_a_locked_account_answers_locked_and_the_lock_fires_once(self, world):
        await world.set_pin()
        await check(world, DURESS)
        for code in (PIN, DURESS, WRONG):
            assert await check(world, code) == PinOutcome(PinVerdict.LOCKED)
        assert len(world.sent) == 2
        assert world.flag() == (False, "duress")

    async def test_a_code_is_never_logged(self, world, caplog):
        caplog.set_level("DEBUG")
        await world.service.set("user-alex", PIN, DURESS)
        await check(world, WRONG)
        await world.service.change("user-alex", "acct-alex", PIN, "0002", "0001")
        await world.service.unlock_session("user-alex", "acct-alex", "sid-alex", "0002")
        await world.service.remove("user-alex", "acct-alex", "0002")
        await world.service.set("user-alex", PIN, DURESS)
        await check(world, DURESS)
        assert "Facts PIN attempt" in caplog.text
        for code in (PIN, DURESS, WRONG):
            assert code not in caplog.text


# ---------------------------------------------------------------------------------------------- change and removal

class TestChangeAndRemove:
    async def test_a_change_with_the_current_pin_replaces_both_codes(self, world):
        await world.set_pin()
        assert await world.service.change("user-alex", "acct-alex", PIN, DURESS, PIN) == DONE
        assert await check(world, DURESS) == PinOutcome(PinVerdict.OK)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is False
        # the old PIN is now the duress code: both codes were replaced, not just one
        assert await check(world, PIN) == PinOutcome(PinVerdict.DURESS, locked_now=True)

    async def test_refused_new_codes_count_no_attempt(self, world):
        await world.set_pin()
        assert await world.service.change("user-alex", "acct-alex", WRONG, "1111", "0001") == PinChange(
            PinChangeStatus.CODES_REFUSED, problem=CodeProblem.TRIVIAL)
        assert world.pins.records["user-alex"].wrong_in_row == 0

    async def test_a_wrong_current_pin_counts_and_changes_nothing(self, world):
        await world.set_pin()
        before = world.pins.records["user-alex"]
        assert await world.service.change("user-alex", "acct-alex", WRONG, "0002", "0001") == PinChange(
            PinChangeStatus.NOT_VERIFIED, attempt=PinOutcome(PinVerdict.WRONG, attempts_left=4))
        assert world.pins.records["user-alex"] == before.model_copy(update={"wrong_in_row": 1})

    async def test_the_duress_code_as_the_current_pin_locks(self, world):
        await world.set_pin()
        result = await world.service.remove("user-alex", "acct-alex", DURESS)
        assert result == PinChange(PinChangeStatus.NOT_VERIFIED, attempt=PinOutcome(PinVerdict.DURESS, locked_now=True))
        assert world.flag() == (False, "duress") and "user-alex" in world.pins.records

    async def test_removal_with_the_current_pin_opens_the_facts_and_tells_the_owner(self, world):
        await world.set_pin()
        world.clock.advance(hours=2)
        assert await world.service.remove("user-alex", "acct-alex", PIN) == DONE
        assert world.pins.records == {}
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is True
        assert world.sent == [("slack", "C-alex", en(UIMessage.FACTS_PIN_REMOVED_NOTICE, time="2026-11-01 11:00 UTC"))]

    @pytest.mark.parametrize("operation", ["change", "remove"])
    async def test_a_record_gone_between_the_check_and_the_write_is_no_pin(self, world, operation):
        await world.set_pin()
        pins = world.pins
        check_once = pins.check

        async def check_then_vanish(*args):
            outcome = await check_once(*args)
            pins.records.pop("user-alex", None)          # a concurrent reset applied right after the right PIN
            return outcome

        pins.check = check_then_vanish
        if operation == "change":
            result = await world.service.change("user-alex", "acct-alex", PIN, "0002", "0001")
        else:
            result = await world.service.remove("user-alex", "acct-alex", PIN)
        assert result == PinChange(PinChangeStatus.NO_PIN) and pins.records == {} and world.sent == []


# ---------------------------------------------------------------------------------------------- the session's unlock

class TestUnlock:
    async def test_the_pin_unlocks_this_session_for_fifteen_minutes(self, world):
        await world.set_pin()
        await world.add_session("sid-mac", hint=MAC)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is False
        assert await world.service.unlock_session("user-alex", "acct-alex", "sid-alex", PIN) == (
            PinOutcome(PinVerdict.OK), True)
        assert world.devices.sessions["sid-alex"].facts_unlocked_until == T0 + timedelta(minutes=15)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is True
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-mac") is False
        world.clock.advance(minutes=15)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is False

    async def test_a_wrong_code_counts_and_unlocks_nothing(self, world):
        await world.set_pin()
        assert await world.service.unlock_session("user-alex", "acct-alex", "sid-alex", WRONG) == (
            PinOutcome(PinVerdict.WRONG, attempts_left=4), False)
        assert world.devices.sessions["sid-alex"].facts_unlocked_until is None

    async def test_without_a_pin_there_is_nothing_to_unlock(self, world):
        assert await world.service.unlock_session("user-alex", "acct-alex", "sid-alex", PIN) == (
            PinOutcome(PinVerdict.NO_PIN), False)

    @pytest.mark.parametrize("sid", ["sid-other", "sid-unknown", "sid-revoked"])
    async def test_a_session_not_live_and_of_this_user_is_refused_before_counting(self, world, sid):
        await world.set_pin()
        await world.add_session("sid-revoked")
        await world.devices.revoke("sid-revoked", SessionRevokeReason.LOGOUT, T0)
        with pytest.raises(PermissionError):
            await world.service.unlock_session("user-alex", "acct-alex", sid, PIN)
        assert world.pins.records["user-alex"].wrong_in_row == 0
        assert await world.service.relock_session("user-alex", sid) is False
        assert await world.service.facts_open("user-alex", "acct-alex", sid) is False

    async def test_relock_closes_the_facts_before_the_unlock_ends(self, world):
        await world.set_pin()
        await world.service.unlock_session("user-alex", "acct-alex", "sid-alex", PIN)
        world.clock.advance(minutes=1)
        assert await world.service.relock_session("user-alex", "sid-alex") is True
        assert world.devices.sessions["sid-alex"].facts_unlocked_until is None
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is False


# ---------------------------------------------------------------------------------------------- a forgotten PIN

class TestReset:
    async def test_a_fresh_google_sign_in_asks_and_the_owner_is_told(self, world):
        await world.set_pin()
        hostile = "Mac — Chrome <!channel> {time}"
        world.clock.advance(minutes=2)
        session = await world.add_session("sid-fresh", hint=hostile, auth_at=T0)
        due = await world.service.request_reset(session)
        assert due == T0 + timedelta(minutes=2, hours=24)
        record = world.pins.records["user-alex"]
        assert (record.reset_requested_at, record.reset_due_at) == (T0 + timedelta(minutes=2), due)
        assert world.sent == [("slack", "C-alex", en(UIMessage.FACTS_PIN_RESET_REQUESTED_NOTICE, platform_hint=hostile,
                                                      time="2026-11-02 09:02 UTC"))]
        assert hostile in world.sent[0][2]

    async def test_asking_again_keeps_the_first_due_time_and_sends_nothing_new(self, world):
        await world.set_pin()
        first = await world.service.request_reset(await world.add_session("sid-fresh"))
        world.clock.advance(minutes=1)
        assert await world.service.request_reset(await world.add_session("sid-again")) == first
        assert len(world.sent) == 1

    @pytest.mark.parametrize("kind, age", [(SessionAuthKind.PASSKEY, timedelta(0)),
                                           (SessionAuthKind.GOOGLE, P.RESET_SIGN_IN_MAX_AGE)])
    async def test_a_passkey_or_stale_sign_in_cannot_ask(self, world, kind, age, caplog):
        await world.set_pin()
        session = await world.add_session("sid-x", auth_kind=kind, auth_at=T0)
        world.clock.advance(seconds=age.total_seconds())
        assert await world.service.request_reset(session) is None
        assert world.pins.records["user-alex"].reset_requested_at is None and world.sent == []
        assert "Facts PIN reset refused" in caplog.text

    async def test_without_a_pin_there_is_nothing_to_reset(self, world):
        assert await world.service.request_reset(await world.add_session("sid-fresh")) is None
        assert world.sent == []

    async def test_cancel_ends_the_pending_reset(self, world):
        await world.set_pin()
        await world.service.request_reset(await world.add_session("sid-fresh"))
        assert await world.service.cancel_reset("user-alex") is True
        assert await world.service.cancel_reset("user-alex") is False
        world.clock.advance(days=2)
        assert await check(world, PIN) == PinOutcome(PinVerdict.OK)

    async def test_once_due_the_next_attempt_finds_no_pin_and_the_owner_is_told(self, world):
        await world.set_pin()
        await world.service.request_reset(await world.add_session("sid-fresh"))
        world.clock.advance(hours=23, minutes=59)
        assert await check(world, PIN) == PinOutcome(PinVerdict.OK)
        world.clock.advance(minutes=1)
        assert await check(world, DURESS) == PinOutcome(PinVerdict.NO_PIN)
        assert world.pins.records == {} and world.flag() == (True, None)
        assert world.sent[-1] == ("slack", "C-alex", en(UIMessage.FACTS_PIN_RESET_DONE_NOTICE,
                                                        time="2026-11-02 09:00 UTC"))
        assert await world.service.set("user-alex", "0002", "0001") == DONE

    async def test_once_due_the_facts_tab_opens_without_a_pin(self, world):
        await world.set_pin()
        await world.service.request_reset(await world.add_session("sid-fresh"))
        world.clock.advance(days=1)
        assert await world.service.facts_open("user-alex", "acct-alex", "sid-alex") is True
        assert world.pins.records == {} and len(world.sent) == 2


# ---------------------------------------------------------------------------------------------- notices

class TestNotices:
    async def test_a_raising_notice_never_fails_the_removal(self, caplog):
        class Broken:
            async def notify_ui_notice(self, *args, **fields):
                raise RuntimeError("channel down")

        world = PinLockWorld(notifications=Broken())
        await world.set_pin()
        assert await world.service.remove("user-alex", "acct-alex", PIN) == DONE
        assert world.pins.records == {}
        assert "Facts PIN notice facts_pin_removed_notice failed" in caplog.text

    async def test_an_undelivered_notice_never_fails_the_removal(self, caplog):
        world = PinLockWorld()                              # no channel stored
        await world.set_pin()
        assert await world.service.remove("user-alex", "acct-alex", PIN) == DONE
        assert "Facts PIN notice facts_pin_removed_notice not delivered" in caplog.text

    async def test_by_default_the_clock_is_now(self, world):
        service = FactsPinService(world.pins, world.devices, world.lock, world.notifier, KEY)
        assert await service.set("user-alex", PIN, DURESS) == DONE
        assert world.pins.records["user-alex"].updated_at > T0
