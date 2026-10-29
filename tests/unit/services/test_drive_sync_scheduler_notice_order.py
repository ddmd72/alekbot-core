"""DriveSyncSchedulerService: the reconnect flag goes up before the notice, and a deliberate disconnect gets
none (USER_DRIVE_INDEXING_RFC §7.7; RFC Δ6)."""
import asyncio

import pytest

from src.domain.drive_index import DriveSyncState, ErrorCategory
from src.domain.user_drive import DriveConnection
from tests.unit.services.test_drive_sync_scheduler_service import env  # noqa: F401  (fixture)


class TestReconnectNoticeReview:
    async def _lost(self, env):  # noqa: F811
        await env.repo.save_sync_state(DriveSyncState(user_id="u", last_error=ErrorCategory.RECONNECT_NEEDED))
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")

    async def test_flag_is_up_before_the_notice_is_sent(self, env):  # noqa: F811
        """The flag is stored before the send (Gmail precedent), so a cancel after the message left
        cannot repeat it."""
        await self._lost(env)
        seen = []
        original = env.notifier.notify

        async def spy(*args, **kwargs):
            seen.append((await env.repo.get_sync_state("u")).reconnect_notified)
            return await original(*args, **kwargs)

        env.notifier.notify = spy
        await env.svc.fan_out()
        assert seen == [True]

    async def test_cancelled_notice_is_not_repeated(self, env):  # noqa: F811
        """A scheduler deadline or deploy SIGTERM cancels the request mid-notify: the message may be out."""
        await self._lost(env)
        env.notifier.raises = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await env.svc.fan_out()
        env.notifier.raises = None
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 1

    async def test_deliberate_disconnect_with_flag_down_sends_no_notice(self, env):  # noqa: F811
        """A user who disconnected on purpose is not told the connection expired (§7.7). Since R1 (SECRETS_AT_REST_RFC
        §6.2, F1) a deliberate disconnect is the Cabinet's mark on the sync state; without the mark the same state gets
        R1's notice (tests/unit/services/test_drive_reconnect_r1.py)."""
        await env.repo.save_sync_state(DriveSyncState(user_id="u"))
        await env.repo.update_sync_flags("u", disconnected_by_user_at=env.clock.now)
        env.drive.connection_state = DriveConnection(connected=False)
        await env.svc.fan_out()
        assert env.notifier.calls == []
        assert (await env.repo.get_sync_state("u")).reconnect_notified is False

    async def test_no_grant_with_flag_up_sends_no_notice(self, env):  # noqa: F811
        """R1's once-per-transition flag (F2): a user already told gets no second notice while the grant stays gone."""
        await env.repo.save_sync_state(DriveSyncState(user_id="u"))
        await env.repo.update_sync_flags("u", reconnect_notified=True)
        env.drive.connection_state = DriveConnection(connected=False)
        await env.svc.fan_out()
        assert env.notifier.calls == []


class TestReconnectNoticeEmptyAnswer:
    """An empty formatter answer is final (owner decision 2026-10-23): no re-ask every 10-minute fan-out."""

    async def _lost(self, env):  # noqa: F811
        await env.repo.save_sync_state(DriveSyncState(user_id="u", last_error=ErrorCategory.RECONNECT_NEEDED))
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")

    async def test_empty_answer_keeps_flag_alerts_once_and_is_not_retried(self, env, caplog):  # noqa: F811
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult
        import logging
        await self._lost(env)
        env.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="empty_answer")
        with caplog.at_level(logging.ERROR):
            await env.svc.fan_out()
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 1
        assert (await env.repo.get_sync_state("u")).reconnect_notified is True
        assert len([p for p in env.alerts.posts if "drive_reconnect_notice" in p]) == 1
        assert len([r for r in caplog.records if getattr(r, "event", None) == "background_empty_answer"]) == 1

    async def test_other_failure_drops_flag_and_retries(self, env):  # noqa: F811
        from src.domain.agent import AgentStatus
        from src.domain.notify_result import NotifyResult
        await self._lost(env)
        env.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="boom")
        await env.svc.fan_out()
        assert (await env.repo.get_sync_state("u")).reconnect_notified is False
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 2
        assert not [p for p in env.alerts.posts if "drive_reconnect_notice" in p]
