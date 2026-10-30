"""Search v2 P5 — the fan-out's ops alerts (USER_DRIVE_INDEXING_RFC §25.4.5 fallback alert, §25.8 as amended by D30):
an index awaiting its v2 rebuild (once), the fallback serving more than FALLBACK_ALERT_SHARE of a day's model cost
(once that day), and a rebuild whose described count has not grown for DESCRIBED_STALL_AFTER while jobs are ready and
budget is left (once, until it grows again)."""
from dataclasses import replace
from datetime import timedelta
from typing import List

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain import drive_index as di
from src.domain.drive_enrichment import InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope
from src.domain.drive_index_params import DESCRIBED_STALL_AFTER
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.unit.services.drive_index_fakes import Clock, FakeDrive, FakeQueue, FakeUsers


class Alerts:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)


@pytest.fixture
def env():
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    alerts = Alerts()

    async def connected():
        return ["u"]

    svc = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), drive=FakeDrive(), users=FakeUsers(),
                                    connected_users=connected, notifier=None, alert_sink=alerts, clock=clock)
    return svc, repo, alerts, clock


def v2_state(clock, **kw) -> DriveSyncState:
    base = dict(user_id="u", created_at=clock.now, last_success_at=clock.now, applied_versions=dict(di.CURRENT_VERSIONS),
                described_grew_at=clock.now)
    base.update(kw)
    return DriveSyncState(**base)


async def test_an_index_awaiting_its_rebuild_alerts_once(env):
    svc, repo, alerts, clock = env
    # a v1 index has neither the Search v2 keys nor `accuracy` (newer still; the gate key since Δ65)
    v1 = {k: v for k, v in di.CURRENT_VERSIONS.items() if k not in {*di.SEARCH_V2_VERSIONS, "accuracy"}}
    repo.states["u"] = v2_state(clock, applied_versions=v1)
    await svc.fan_out()
    await svc.fan_out()
    assert len([p for p in alerts.posts if "rebuild" in p]) == 1 and repo.states["u"].migration_alerted


async def test_a_fresh_or_rebuilt_index_does_not_alert(env):
    svc, repo, alerts, clock = env
    repo.states["u"] = v2_state(clock)
    await svc.fan_out()
    repo.states["u"] = DriveSyncState(user_id="u", connection_id="u", created_at=clock.now)   # the fan-out's minimal state
    await svc.fan_out()
    assert not [p for p in alerts.posts if "rebuild" in p]


async def test_the_fallback_serving_most_of_a_days_cost_alerts_once_that_day(env):
    svc, repo, alerts, clock = env
    repo.states["u"] = v2_state(clock)
    day = clock.now.date().isoformat()
    repo.usage[("u", clock.now.strftime("%Y-%m"))] = ModelUsageMonth(daily={day: (1.0, 0.6)})
    await svc.fan_out()
    await svc.fan_out()
    assert len([p for p in alerts.posts if "fallback" in p]) == 1
    assert repo.states["u"].fallback_alerted_day == day


async def test_a_small_fallback_share_does_not_alert(env):
    svc, repo, alerts, clock = env
    repo.states["u"] = v2_state(clock)
    repo.usage[("u", clock.now.strftime("%Y-%m"))] = ModelUsageMonth(daily={clock.now.date().isoformat(): (1.0, 0.4)})
    await svc.fan_out()
    assert not [p for p in alerts.posts if "fallback" in p]


def _ready_record() -> DriveIndexRecord:
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="d1", name="a.pdf", kind="pdf", scope=Scope.NORMAL,
                            state=RecordState.INDEXED, indexed_generation=1, profile_state="ready",
                            enrich_ready=True)


async def test_a_stalled_rebuild_alerts_once_and_clears_when_it_grows(env):
    svc, repo, alerts, clock = env
    repo.states["u"] = v2_state(clock, described_grew_at=clock.now - DESCRIBED_STALL_AFTER - timedelta(minutes=1))
    repo.records[("u", "d1")] = _ready_record()
    await svc.fan_out()
    await svc.fan_out()
    assert len([p for p in alerts.posts if "stalled" in p]) == 1 and repo.states["u"].stall_alerted
    repo.states["u"] = replace(repo.states["u"], described_grew_at=clock.now)
    await svc.fan_out()
    assert not repo.states["u"].stall_alerted


async def test_no_stall_alert_without_budget_or_ready_jobs(env):
    svc, repo, alerts, clock = env
    old = clock.now - DESCRIBED_STALL_AFTER - timedelta(minutes=1)
    repo.states["u"] = v2_state(clock, described_grew_at=old)
    await svc.fan_out()                                                    # nothing ready
    repo.records[("u", "d1")] = _ready_record()
    repo.initial["u"] = InitialBuild(allowance_usd=40.0, spent_usd=40.0, closed_at=clock.now)
    repo.usage[("u", clock.now.strftime("%Y-%m"))] = ModelUsageMonth(profile_usd=4.0, passage_usd=20.0)
    await svc.fan_out()                                                    # no budget left
    assert not [p for p in alerts.posts if "stalled" in p]
