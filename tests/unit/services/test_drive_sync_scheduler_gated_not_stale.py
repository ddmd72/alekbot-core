"""A5: a sync state awaiting the owner's rebuild (no migration gate key) does no tick work by design and has its own
alert, so the staleness alert stays quiet for it (USER_DRIVE_INDEXING_RFC §25.8, D30)."""
from src.domain.drive_index import DriveSyncState
from src.domain.drive_index_params import STALE_ALERT_AFTER

from .test_drive_sync_scheduler_service import env  # noqa: F401  (fixture)
from .drive_index_fakes import U_SCOPE


async def test_a_gated_state_raises_no_staleness_alert(env):  # noqa: F811
    await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now,
                                                  applied_versions={"policy": 2}))
    env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
    await env.svc.fan_out()
    assert not [p for p in env.alerts.posts if "stale" in p.lower() or "no clean tick" in p.lower()]
    assert (await env.repo.get_sync_state(U_SCOPE)).stale_alerted is False
