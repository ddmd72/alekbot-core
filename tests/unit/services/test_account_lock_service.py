"""AccountLockService (CABINET_DEVICE_SESSIONS_RFC §7.8 steps 2–4) in `tests/support/pin_lock_world.py`: after the flag,
every session and passkey of the user is revoked with reason `account_lock`, a pending PIN reset is cancelled, every MCP
grant of the user is revoked, and the lock notice reaches every stored channel; each step runs whatever the one before
did, and no other user is touched. (The flag itself is the PIN transaction's: `test_facts_pin_store_kit.py`; the lock
fired through a real attempt: `test_facts_pin_service.py`.)"""
import pytest

from src.domain.device_session import SessionRevokeReason
from src.domain.ui_messages import UIMessage
from src.domain.user import InactiveReason
from src.services.account_lock_service import AccountLockService
from tests.support.pin_lock_world import T0, PinLockWorld, en


@pytest.fixture
async def world():
    w = PinLockWorld()
    await w.seed()
    await w.set_pin()
    await w.service.request_reset(await w.add_session("sid-fresh"))
    w.channels.channels.clear()
    return w


@pytest.mark.parametrize("reason, notice", [
    (InactiveReason.DURESS, UIMessage.ACCOUNT_LOCKED_DURESS_NOTICE),
    (InactiveReason.PIN_ATTEMPTS, UIMessage.ACCOUNT_LOCKED_PIN_ATTEMPTS_NOTICE),
])
async def test_the_lock_revokes_everything_of_the_user_and_tells_every_channel(world, reason, notice):
    await world.lock.lock("user-alex", reason)
    sessions = world.devices.sessions
    assert [(sid, s.revoke_reason) for sid, s in sessions.items() if s.user_id == "user-alex"] == [
        ("sid-alex", SessionRevokeReason.ACCOUNT_LOCK), ("sid-fresh", SessionRevokeReason.ACCOUNT_LOCK)]
    assert world.devices.passkeys["key-alex"].revoked_at == T0
    assert world.pins.records["user-alex"].reset_requested_at is None
    assert world.token_revoked("user-alex")
    assert world.sent == [("slack", "C-alex", en(notice)), ("telegram", "chat-alex", en(notice))]
    assert sessions["sid-other"].revoked_at is None and not world.token_revoked("user-other")
    assert world.pins.records["user-alex"].pin_digest                   # the PIN and the facts stay


async def test_a_failed_revocation_still_revokes_the_grants_and_posts_the_notice(world, caplog):
    async def down(user_id, reason, now):
        raise RuntimeError("store down")

    world.devices.revoke_all = down
    await world.lock.lock("user-alex", InactiveReason.DURESS)
    assert world.token_revoked("user-alex")
    assert len(world.sent) == 2
    assert "revoking the sessions or connector grants failed" in caplog.text


async def test_a_failed_notice_is_logged(world, caplog):
    class Broken:
        async def notify_ui_notice_all_channels(self, user_id, message, **fields):
            raise RuntimeError("channel down")

    lock = AccountLockService(world.device_sessions, Broken())
    await lock.lock("user-alex", InactiveReason.PIN_ATTEMPTS)
    assert world.devices.sessions["sid-alex"].revoke_reason is SessionRevokeReason.ACCOUNT_LOCK
    assert "the lock notice failed" in caplog.text


async def test_a_lock_notice_with_no_channel_is_logged(caplog):
    world = PinLockWorld()                                  # no channel stored
    await world.lock.lock("user-alex", InactiveReason.DURESS)
    assert world.sent == []
    assert "the lock notice found no channel" in caplog.text
