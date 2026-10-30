"""The drive reconnect notice is retried when nothing reached the user (USER_DRIVE_INDEXING_RFC §7.7): the flag goes
up before `notify` and comes down whenever `NotifyResult` says no send was attempted, a formatter failure included.
Once a send was attempted it stays up, so a partial delivery is never repeated every fan-out. Per connection
(MULTI_DRIVE_RFC §4.4): the lost grant is a NEEDS_RECONNECT connection."""
import pytest

from src.domain.agent import AgentStatus
from src.domain.drive_index import ErrorCategory
from src.domain.notify_result import NotifyResult

from .drive_reconnect_world import CONN, USER, ReconnectWorld


@pytest.fixture
def env():
    return ReconnectWorld()


async def _lost(ctx: ReconnectWorld) -> None:
    await ctx.connected(last_error=ErrorCategory.RECONNECT_NEEDED)
    await ctx.connections.flag_reconnect((await ctx.connections.get(USER, CONN)).handle)


@pytest.mark.parametrize("status", [AgentStatus.TIMEOUT, AgentStatus.FAILED])
async def test_formatter_failure_is_retried_on_the_next_fan_out(env, status):
    await _lost(env)
    env.notifier.result = NotifyResult(delivered=False, agent_status=status, error="formatter")
    await env.svc.fan_out()
    assert (await env.state()).reconnect_notified is False
    env.notifier.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS, sent=True)
    for _ in range(3):
        env.clock.advance(600)
        await env.svc.fan_out()
    assert len(env.notifier.calls) == 2
    assert (await env.state()).reconnect_notified is True


async def test_an_attempted_send_that_failed_is_not_repeated(env):
    await _lost(env)
    env.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="slack down",
                                       sent=True)
    await env.svc.fan_out()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert len(env.notifier.calls) == 1
    assert (await env.state()).reconnect_notified is True
