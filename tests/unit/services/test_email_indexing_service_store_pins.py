"""Defect of the email indexing watchdog that the job store's blind update exposes (P11-G3-5), pinned through the real
`EmailIndexingService` over the real `FirestoreEmailJobRepository` on the evaluating Firestore fake (H1).

The race is forced, not gathered: the job repository's stale query is wrapped so that a completing `update_job` lands
right after the query returns, which is the window between the watchdog's read and its write.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.domain.email import IndexingJob
from src.services.email_indexing_service import EmailIndexingService
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require
from src.composition.connector_registry import ConnectorRegistry

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
JOBS = "eval_email_indexing_jobs_v1"
NOW = UTC_MIDNIGHT.utc
THRESHOLD = NOW - timedelta(hours=2)


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={JOBS: "development_email_indexing_jobs_v1"})
        self.jobs = FirestoreEmailJobRepository(self.db, SimpleNamespace(email_indexing_jobs_collection=JOBS))
        self.service = _service(self.jobs)
        self.returned: list = []           # the job ids the stale query returned
        self.completed: list = []          # the job ids a completing update landed on, right after the query

    @property
    def docs(self) -> dict:
        return self.db.collection(JOBS).docs

    def complete_right_after_the_query(self) -> None:
        query = self.jobs.get_stale_running_jobs

        async def completing_after_the_query(updated_before: datetime):
            stale = await query(updated_before)
            self.returned += [j.job_id for j in stale]
            for stale_job in stale:
                await self.jobs.update_job(stale_job.job_id, {"status": "completed", "completed_at": NOW})
                self.completed.append(stale_job.job_id)
            return stale

        self.jobs.get_stale_running_jobs = completing_after_the_query


def _service(jobs) -> EmailIndexingService:
    return EmailIndexingService(None, None, jobs, None, None, None, None, connector=ConnectorRegistry().get("gmail"))


async def _silent_running_job(world: World, job_id: str = "j1") -> None:
    await world.jobs.create_job(IndexingJob(
        job_id=job_id, user_id="u1", account_id="a1", provider="gmail", triggered_by="scheduler", status="running",
        started_at=NOW - timedelta(hours=4), updated_at=NOW - timedelta(hours=3)))


@pytest.fixture
def world():
    return World()


async def test_a_job_silent_for_two_hours_is_marked_failed(world):
    """The passing control of P11-G3-5: a truly stale job is failed."""
    await _silent_running_job(world)
    assert await world.service.mark_stale_jobs_failed(THRESHOLD) == 1
    assert world.docs["j1"]["status"] == "failed"


async def test_a_fresh_running_job_is_left_alone(world):
    await world.jobs.create_job(IndexingJob(job_id="j2", user_id="u1", provider="gmail", triggered_by="scheduler", status="running",
                                            started_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(minutes=5)))
    assert await world.service.mark_stale_jobs_failed(THRESHOLD) == 0
    assert world.docs["j2"]["status"] == "running"


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G3-5: the watchdog overwrites a job that completed after its stale query")
async def test_a_job_that_completes_after_the_stale_query_stays_completed(world):
    """The watchdog lists the stale running jobs, then writes `failed` with no precondition, so a job that completed in
    between is shown as failed. Flipping fix: a `last_update_time` precondition on the watchdog's update, or a
    transaction that re-checks `status == "running"`; a fix that raises when the job moved passes too (the exception is
    swallowed here), a fix that skips it passes. A fix that replaces the stale query (query and conditional update in
    one repository method) never calls the wrapped read and reports FAILED: re-point the hook to the new read."""
    await _silent_running_job(world)
    world.complete_right_after_the_query()
    try:
        await world.service.mark_stale_jobs_failed(THRESHOLD)
    except Exception:
        pass
    require(world.returned == ["j1"], "the query returned the job as stale")
    require(world.completed == ["j1"], "the job completed after the query returned")
    assert world.docs["j1"]["status"] == "completed", f"stored status: {world.docs['j1']['status']!r}"
