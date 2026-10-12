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
        """A user who disconnected on purpose is not told the connection expired (§7.7)."""
        await env.repo.save_sync_state(DriveSyncState(user_id="u"))
        env.drive.connection_state = DriveConnection(connected=False)
        await env.svc.fan_out()
        assert env.notifier.calls == []
