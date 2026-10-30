"""Gmail auto-index fan-out on the DST transition days (P16-C item 4, P16-D1).

`EmailIndexingService.start_indexing_for_eligible_users` runs on an hourly tick and creates a job when the user's
local hour equals `gmail_auto_index_hour`. The sweep runs the tick for every UTC hour of the transition's local day,
the way the Cloud Scheduler does, and the worker finishes each job between ticks (otherwise the "running" guard hides
a second job). Fakes: the real OAuth adapter over the evaluating Firestore fake, an in-memory job journal.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from src.domain.email import IndexingJob
from src.domain.user import UserBotConfig, UserProfile
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from src.services.email_indexing_service import EmailIndexingService
from tests.support.clock_instants import BERLIN, HELSINKI, NEW_YORK, REPEATED_HOUR, SKIPPED_HOUR, UTC, ZONES
from tests.support.oauth_credentials_store import CredentialsStore, creds
from src.composition.connector_registry import ConnectorRegistry


class _Journal(EmailIndexingJobRepository):
    """The job journal in memory; `created` keeps every job ever created, in order."""

    def __init__(self) -> None:
        self.jobs: Dict[str, IndexingJob] = {}
        self.created: List[IndexingJob] = []

    async def create_job(self, job: IndexingJob) -> None:
        self.jobs[job.job_id] = job
        self.created.append(job)

    async def update_job(self, job_id: str, updates: Dict[str, Any]) -> None:
        self.jobs[job_id] = self.jobs[job_id].model_copy(update=updates)

    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        return self.jobs.get(job_id)

    async def get_latest_job(self, user_id: str, provider: str) -> Optional[IndexingJob]:
        mine = [j for j in self.created if j.user_id == user_id and j.provider == provider]
        return self.jobs[mine[-1].job_id] if mine else None

    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        return [j for j in reversed(self.created) if j.user_id == user_id][:limit]

    async def get_stale_running_jobs(self, updated_before: datetime) -> List[IndexingJob]:
        return [j for j in self.jobs.values() if j.status == "running" and j.updated_at < updated_before]

    async def finish_running(self) -> None:
        for job_id, job in list(self.jobs.items()):
            if job.status == "running":
                await self.update_job(job_id, {"status": "completed"})


class _Users:
    def __init__(self, *profiles: UserProfile) -> None:
        self._by_id = {p.user_id: p for p in profiles}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self._by_id.get(user_id)


def _profile(user_id: str, zone: str, hour: int) -> UserProfile:
    return UserProfile(user_id=user_id, account_id=f"a-{user_id}",
                       config=UserBotConfig(timezone=zone, gmail_auto_index=True, gmail_auto_index_hour=hour))


async def _service(*profiles: UserProfile):
    store = CredentialsStore()
    for p in profiles:
        await store.repo.save(creds(p.user_id, "gmail"))
    journal = _Journal()
    service = EmailIndexingService(gmail=None, email_repo=None, job_repo=journal, exclusions_repo=None,  # type: ignore[arg-type]
                                   classifier=None, embedding=None, oauth=store.repo, connector=ConnectorRegistry().get("gmail"))  # type: ignore[arg-type]
    return service, journal, _Users(*profiles)


def _local_day_ticks(zone: str, local_day: datetime) -> List[datetime]:
    """Every hourly tick (minute 12) from local midnight to the next local midnight: 23, 24 or 25 of them."""
    tz = ZoneInfo(zone)
    start = local_day.replace(hour=0, minute=0, tzinfo=tz).astimezone(UTC)
    end = (local_day + timedelta(days=1)).replace(hour=0, minute=0, tzinfo=tz).astimezone(UTC)
    ticks, t = [], start + timedelta(minutes=12)
    while t < end:
        ticks.append(t)
        t += timedelta(hours=1)
    return ticks


async def _job_ticks_on_local_day(zone: str, local_day: datetime, hour: int) -> List[datetime]:
    """The ticks (UTC) that created a job, in order."""
    service, journal, users = await _service(_profile("u1", zone, hour))
    fired = []
    for tick in _local_day_ticks(zone, local_day):
        before = len(journal.created)
        await service.start_indexing_for_eligible_users(users, tick)
        fired.extend([tick] * (len(journal.created) - before))
        await journal.finish_running()
    return fired


async def _jobs_on_local_day(zone: str, local_day: datetime, hour: int) -> int:
    return len(await _job_ticks_on_local_day(zone, local_day, hour))


def _assert_fired_in_the_configured_local_hour(zone: str, ticks: List[datetime], hour: int) -> None:
    """Guard that outlives the xfail marks: a job is created by a tick whose LOCAL hour is the configured one."""
    assert [t.astimezone(ZoneInfo(zone)).hour for t in ticks] == [hour] * len(ticks)


_SPRING_DAY = {z: datetime(w.naive.year, w.naive.month, w.naive.day) for z, w in SKIPPED_HOUR.items()}
_FALL_DAY = {z: datetime(r.wall.year, r.wall.month, r.wall.day) for z, r in REPEATED_HOUR.items()}
_D1 = dict(strict=True, raises=AssertionError,
           reason="P16-D1: the auto-index hour is matched by equality on the local hour; a skipped hour never fires, a repeated hour fires twice, and one bad zone aborts the fan-out; owner decision")


@pytest.mark.parametrize("zone", ZONES)
async def test_an_ordinary_hour_creates_one_job_on_each_transition_day(zone):
    for day in (_SPRING_DAY[zone], _FALL_DAY[zone]):
        ticks = await _job_ticks_on_local_day(zone, day, 8)
        assert len(ticks) == 1
        _assert_fired_in_the_configured_local_hour(zone, ticks, 8)


@pytest.mark.parametrize("zone", [pytest.param(z, marks=pytest.mark.xfail(**_D1)) for z in ZONES])
async def test_the_skipped_hour_still_creates_one_job_that_local_day(zone):
    hour = SKIPPED_HOUR[zone].naive.hour
    # No local-hour guard here: a fix may fire the skipped hour's job at the next local hour.
    assert await _jobs_on_local_day(zone, _SPRING_DAY[zone], hour) == 1


@pytest.mark.parametrize("zone", [pytest.param(z, marks=pytest.mark.xfail(**_D1)) for z in ZONES])
async def test_the_repeated_hour_creates_one_job_that_local_day(zone):
    hour = REPEATED_HOUR[zone].wall.hour
    ticks = await _job_ticks_on_local_day(zone, _FALL_DAY[zone], hour)
    _assert_fired_in_the_configured_local_hour(zone, ticks, hour)
    assert len(ticks) == 1


@pytest.mark.parametrize("zone", [BERLIN, HELSINKI, NEW_YORK])
async def test_a_running_job_blocks_the_second_pass_of_the_repeated_hour(zone):
    """The guard the card names: without the worker finishing the first job, the repeated hour makes only one."""
    service, journal, users = await _service(_profile("u1", zone, REPEATED_HOUR[zone].wall.hour))
    for tick in _local_day_ticks(zone, _FALL_DAY[zone]):
        await service.start_indexing_for_eligible_users(users, tick)
    assert len(journal.created) == 1


@pytest.mark.xfail(**_D1)
async def test_one_user_with_an_unknown_zone_does_not_stop_the_next_user():
    service, journal, users = await _service(_profile("u1", "Mars/Base", 8), _profile("u2", BERLIN, 8))
    assert await service._oauth.list_user_ids("gmail") == ["u1", "u2"], "the bad zone must come first"
    tick = datetime(2026, 10, 27, 8, 12, tzinfo=ZoneInfo(BERLIN)).astimezone(UTC)
    refused = False
    try:
        await service.start_indexing_for_eligible_users(users, tick)
    except ZoneInfoNotFoundError:
        refused = True
    assert [j.user_id for j in journal.created] == ["u2"], f"second user's job missing (fan-out aborted: {refused})"
