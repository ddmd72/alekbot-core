"""The drive reconnect notice is retried when nothing reached the user (USER_DRIVE_INDEXING_RFC §7.7): the flag goes
up before `notify` and comes down whenever `NotifyResult` says no send was attempted, a formatter failure included.
Once a send was attempted it stays up, so a partial delivery is never repeated every fan-out."""
import pytest

from src.domain.agent import AgentStatus
from src.domain.drive_index import DriveSyncState, ErrorCategory
from src.domain.notify_result import NotifyResult
from src.domain.user_drive import DriveConnection
from tests.unit.services.test_drive_sync_scheduler_service import env  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE


async def _lost(ctx):
    await ctx.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_error=ErrorCategory.RECONNECT_NEEDED))
    ctx.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")


@pytest.mark.parametrize("status", [AgentStatus.TIMEOUT, AgentStatus.FAILED])
async def test_formatter_failure_is_retried_on_the_next_fan_out(env, status):  # noqa: F811
    await _lost(env)
    env.notifier.result = NotifyResult(delivered=False, agent_status=status, error="formatter")
    await env.svc.fan_out()
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
    env.notifier.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
    for _ in range(3):
        env.clock.advance(600)
        await env.svc.fan_out()
    assert len(env.notifier.calls) == 2
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True


async def test_an_attempted_send_that_failed_is_not_repeated(env):  # noqa: F811
    await _lost(env)
    env.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="slack down",
                                       sent=True)
    await env.svc.fan_out()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert len(env.notifier.calls) == 1
    assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True
