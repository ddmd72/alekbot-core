"""Drive's R1 notice in the drive fan-out (SECRETS_AT_REST_RFC §6.2 R1, F1; plan W2.3), per connection (MULTI_DRIVE_RFC
§4.4): an ACTIVE connection whose grant is gone altogether, with a sync state showing a past index, gets R1's one
notice under the same flag as the lost-grant notice; a disconnected connection never does (a deliberate disconnect
ends the connection); a working grant clears the flag. Runs on the fan-out's reconnect world (`drive_reconnect_world`)."""
import pytest

from src.domain.integration import Integration
from src.domain.notification_kind import NotificationKind
from src.domain.reconnect_policy import R1_NOTICES
from src.services.drive_sync_scheduler_service import RECONNECT_NOTICE

from .drive_reconnect_world import CONN, USER, ReconnectWorld


@pytest.fixture
def env():
    return ReconnectWorld()


async def _past_connection_no_grant(ctx: ReconnectWorld) -> None:
    await ctx.connected()
    ctx.credential_gone()


async def test_no_grant_and_a_past_connection_sends_one_r1_notice(env):
    await _past_connection_no_grant(env)
    summary = await env.svc.fan_out()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert summary["notices"] == 1
    assert env.notifier.calls == [{"user_id": USER, "account_id": "acc-1", "alert": R1_NOTICES[Integration.DRIVE],
                                   "kind": NotificationKind.DOCUMENT_DELIVERY}]
    assert (await env.state()).reconnect_notified is True


async def test_a_deliberate_disconnect_sends_none(env):
    await _past_connection_no_grant(env)
    await env.connections.disconnect(USER, CONN)          # a disconnect ends the connection (MULTI_DRIVE_RFC §4.4)
    summary = await env.svc.fan_out()
    assert summary["notices"] == 0 and env.notifier.calls == []
    assert (await env.state()).reconnect_notified is False


async def test_a_consent_then_a_working_grant_clears_the_flag_and_the_next_loss_notifies(env):
    await _past_connection_no_grant(env)
    await env.svc.fan_out()
    env.credential_gone(False)
    env.clock.advance(600)
    await env.svc.fan_out()
    assert (await env.state()).reconnect_notified is False
    env.credential_gone()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert [c["alert"] for c in env.notifier.calls] == [R1_NOTICES[Integration.DRIVE]] * 2


async def test_a_lost_grant_keeps_its_own_notice(env):
    await env.connected()
    await env.connections.flag_reconnect((await env.connections.get(USER, CONN)).handle)
    await env.svc.fan_out()
    assert [c["alert"] for c in env.notifier.calls] == [RECONNECT_NOTICE]


async def test_a_user_with_indexing_off_gets_no_notice(env):
    await env.connected(indexing=False)
    env.credential_gone()
    await env.svc.fan_out()
    assert env.notifier.calls == []
