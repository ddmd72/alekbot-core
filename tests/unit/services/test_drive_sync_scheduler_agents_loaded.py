"""The reconnect notice loads the user's agents before `notify`
(USER_DRIVE_INDEXING_RFC §7.7; RFC Δ6). The formatter agent is eager and only registered while the
user's agent set is cached, so on a cold start or after idle eviction `route_message` answers
"Unknown recipient" and the one notice is lost unless the agents are loaded first."""
from typing import List

import pytest

from src.domain.drive_index import DriveSyncState, ErrorCategory
from src.domain.user_drive import DriveConnection
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.unit.services.test_drive_sync_scheduler_service import env  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE


def _with_ensure_agents(env, ensure_agents):  # noqa: F811
    async def connected_users():
        return list(env.connected)

    return DriveSyncSchedulerService(repo=env.repo, queue=env.queue, drive=env.drive, users=env.users,
                                     connected_users=connected_users, notifier=env.notifier,
                                     alert_sink=env.alerts, clock=env.clock, ensure_agents=ensure_agents)


async def _lost_grant(env):  # noqa: F811
    await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_error=ErrorCategory.RECONNECT_NEEDED))
    env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")


class TestReconnectNoticeLoadsAgents:
    async def test_agents_are_loaded_before_the_notice(self, env):  # noqa: F811
        await _lost_grant(env)
        order: List[tuple] = []

        async def ensure_agents(user_id: str) -> dict:
            order.append(("ensure_agents", user_id))
            return {}

        original = env.notifier.notify

        async def notify(*args, **kwargs):
            order.append(("notify", kwargs["user_id"]))
            return await original(*args, **kwargs)

        env.notifier.notify = notify
        summary = await _with_ensure_agents(env, ensure_agents).fan_out()
        assert order == [("ensure_agents", "u"), ("notify", "u")]
        assert summary["notices"] == 1

    async def test_failed_agent_load_keeps_the_flag_down_and_sends_nothing(self, env):  # noqa: F811
        await _lost_grant(env)

        async def ensure_agents(user_id: str) -> dict:
            raise RuntimeError("agent build failed")

        summary = await _with_ensure_agents(env, ensure_agents).fan_out()
        assert env.notifier.calls == []
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
        assert summary["errors"] == 1

    async def test_failed_agent_load_is_retried_on_the_next_fan_out(self, env):  # noqa: F811
        await _lost_grant(env)
        attempts: List[str] = []

        async def ensure_agents(user_id: str) -> dict:
            attempts.append(user_id)
            if len(attempts) == 1:
                raise RuntimeError("agent build failed")
            return {}

        svc = _with_ensure_agents(env, ensure_agents)
        await svc.fan_out()
        env.clock.advance(600)
        await svc.fan_out()
        assert attempts == ["u", "u"]
        assert len(env.notifier.calls) == 1
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True
