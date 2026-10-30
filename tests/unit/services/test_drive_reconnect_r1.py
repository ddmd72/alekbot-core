"""Drive's R1 notice in the drive fan-out (SECRETS_AT_REST_RFC §6.2 R1, F1; plan W2.3): with no grant at all while the
sync state shows a past connection, the fan-out sends R1's one notice under the same flag as the lost-grant notice,
skips a state marked "disconnected by user", and a working grant clears the flag. Runs on the drive fan-out's own test
world (in-memory repository, a drive per user, the recording notifier)."""
from src.domain.drive_index import DriveSyncState
from src.domain.notification_kind import NotificationKind
from src.domain.integration import Integration
from src.domain.reconnect_policy import R1_NOTICES, Cleared
from src.domain.user_drive import DriveConnection
from src.services.drive_sync_scheduler_service import RECONNECT_NOTICE
from tests.unit.services.test_drive_sync_scheduler_service import env  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE

NO_GRANT = DriveConnection(connected=False)


async def _past_connection_no_grant(ctx):
    await ctx.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
    ctx.drive.connection_state = NO_GRANT
    ctx.connected.clear()   # no credential holder: the user is visited because of the stored state


async def test_no_grant_and_a_past_connection_sends_one_r1_notice(env):  # noqa: F811
    await _past_connection_no_grant(env)
    summary = await env.svc.fan_out()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert summary["notices"] == 1
    assert env.notifier.calls == [{"user_id": "u", "account_id": "a", "alert": R1_NOTICES[Integration.DRIVE],
                                   "kind": NotificationKind.DOCUMENT_DELIVERY}]
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True


async def test_a_deliberate_disconnect_sends_none(env):  # noqa: F811
    await _past_connection_no_grant(env)
    await env.repo.update_sync_flags(U_SCOPE, disconnected_by_user_at=env.clock.now)
    summary = await env.svc.fan_out()
    assert summary["notices"] == 0 and env.notifier.calls == []
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False


async def test_a_consent_then_a_working_grant_clears_the_flag_and_the_next_loss_notifies(env):  # noqa: F811
    await _past_connection_no_grant(env)
    await env.svc.fan_out()
    env.drive.connection_state = DriveConnection(connected=True, drive_id="d1")
    env.clock.advance(600)
    await env.svc.fan_out()
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
    env.drive.connection_state = NO_GRANT
    env.clock.advance(600)
    await env.svc.fan_out()
    assert [c["alert"] for c in env.notifier.calls] == [R1_NOTICES[Integration.DRIVE]] * 2


async def test_a_cleared_mark_lets_r1_notify_again(env):  # noqa: F811
    await _past_connection_no_grant(env)
    await env.repo.update_sync_flags(U_SCOPE, disconnected_by_user_at=env.clock.now)
    await env.svc.fan_out()
    await env.repo.update_sync_flags(U_SCOPE, disconnected_by_user_at=Cleared.MARK)
    env.clock.advance(600)
    await env.svc.fan_out()
    assert [c["alert"] for c in env.notifier.calls] == [R1_NOTICES[Integration.DRIVE]]


async def test_a_lost_grant_keeps_its_own_notice(env):  # noqa: F811
    await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
    env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
    await env.svc.fan_out()
    assert [c["alert"] for c in env.notifier.calls] == [RECONNECT_NOTICE]


async def test_a_user_with_indexing_off_gets_no_notice(env):  # noqa: F811
    await _past_connection_no_grant(env)
    env.users.enabled["u"] = False
    await env.svc.fan_out()
    assert env.notifier.calls == []
