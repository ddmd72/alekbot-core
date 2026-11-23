"""A lock whose revocation failed is completed by the next PIN attempt against the locked account (any surface):
the idempotent revocation runs again, the lock notice never does."""
import pytest

from src.domain.facts_pin_policy import PinOutcome, PinSurface, PinVerdict
from tests.support.pin_lock_world import PIN, WRONG, PinLockWorld


@pytest.fixture
async def world():
    w = PinLockWorld()
    await w.seed()
    await w.set_pin()
    return w


async def test_a_failed_first_revoke_is_completed_on_the_next_attempt_without_a_second_notice(world):
    real = world.devices.revoke_all

    async def down(user_id, reason, now):
        raise RuntimeError("store down")

    world.devices.revoke_all = down
    for _ in range(5):
        outcome = await world.service.check("user-alex", "acct-alex", WRONG, PinSurface.CHAT)
    assert outcome == PinOutcome(PinVerdict.LOCKED, locked_now=True)
    assert world.devices.sessions["sid-alex"].revoked_at is None
    notices = len(world.sent)
    assert notices > 0

    world.devices.revoke_all = real
    again = await world.service.check("user-alex", "acct-alex", PIN, PinSurface.CABINET)
    assert again == PinOutcome(PinVerdict.LOCKED)
    assert world.devices.sessions["sid-alex"].revoked_at is not None
    assert world.token_revoked("user-alex")
    assert len(world.sent) == notices


async def test_an_attempt_on_a_user_without_a_lock_revokes_nothing(world):
    await world.service.check("user-alex", "acct-alex", WRONG, PinSurface.CHAT)
    assert world.devices.sessions["sid-alex"].revoked_at is None
