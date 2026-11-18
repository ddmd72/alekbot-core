"""New store capabilities of the job journal (P15-D6, P11-G3-5): `create_job_if_absent` and the status-conditional
`update_job`, run on `FirestoreEmailJobRepository` over the evaluating Firestore fake. The same contract is checked on
the port's default `create_job_if_absent` through a journal in memory."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from google.api_core.exceptions import NotFound

from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.domain.email import IndexingJob
from src.ports.email_indexing_job_repository import EmailIndexingJobRepository
from tests.support.clock_instants import UTC_MIDNIGHT

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
JOBS = "eval_email_indexing_jobs_v1"
T = UTC_MIDNIGHT.utc


def _job(job_id="j1", status="running", **over) -> IndexingJob:
    fields = dict(job_id=job_id, user_id="u1", account_id="a1", provider="gmail", triggered_by="scheduler",
                  status=status, started_at=T, updated_at=T)
    fields.update(over)
    return IndexingJob(**fields)


@pytest.fixture
def repo_and_docs(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={JOBS: "development_email_indexing_jobs_v1"})
    repo = FirestoreEmailJobRepository(db, SimpleNamespace(email_indexing_jobs_collection=JOBS))
    return repo, db.collection(JOBS).docs


async def test_create_if_absent_stores_the_job_once(repo_and_docs):
    repo, docs = repo_and_docs
    assert await repo.create_job_if_absent(_job(emails_stored=1)) is True
    assert await repo.create_job_if_absent(_job(emails_stored=99)) is False
    assert docs["j1"]["emails_stored"] == 1


async def test_conditional_update_applies_while_the_status_matches(repo_and_docs):
    repo, docs = repo_and_docs
    await repo.create_job(_job())
    assert await repo.update_job("j1", {"status": "failed"}, expected_status="running") is True
    assert docs["j1"]["status"] == "failed"


async def test_conditional_update_leaves_a_job_that_moved_on(repo_and_docs):
    repo, docs = repo_and_docs
    await repo.create_job(_job(status="completed"))
    assert await repo.update_job("j1", {"status": "failed"}, expected_status="running") is False
    assert docs["j1"]["status"] == "completed"


async def test_conditional_update_rereads_when_another_writer_got_in_between(repo_and_docs):
    """A page's progress write lands between the read and the precondition write: the job is still running, so the
    update is applied on the second read."""
    repo, docs = repo_and_docs
    await repo.create_job(_job())
    real_get = repo.collection.document("j1").__class__.get
    bumped: List[bool] = []

    async def get_then_bump(self, *a, **k):
        snap = await real_get(self, *a, **k)
        if not bumped:
            bumped.append(True)
            await repo.update_job("j1", {"emails_stored": 5})
        return snap

    repo.collection.document("j1").__class__.get = get_then_bump
    try:
        assert await repo.update_job("j1", {"status": "failed"}, expected_status="running") is True
    finally:
        repo.collection.document("j1").__class__.get = real_get
    assert docs["j1"]["status"] == "failed" and docs["j1"]["emails_stored"] == 5


async def test_conditional_update_of_a_missing_job_raises_not_found(repo_and_docs):
    repo, _ = repo_and_docs
    with pytest.raises(NotFound):
        await repo.update_job("nope", {"status": "failed"}, expected_status="running")


class _Journal(EmailIndexingJobRepository):
    def __init__(self) -> None:
        self.jobs: Dict[str, IndexingJob] = {}

    async def create_job(self, job: IndexingJob) -> None:
        self.jobs[job.job_id] = job

    async def create_job_if_absent(self, job: IndexingJob) -> bool:
        if job.job_id in self.jobs:
            return False
        self.jobs[job.job_id] = job
        return True

    async def update_job(self, job_id: str, updates: Dict[str, Any], expected_status: Optional[str] = None) -> bool:
        return True

    async def get_job(self, job_id: str) -> Optional[IndexingJob]:
        return self.jobs.get(job_id)

    async def get_latest_job(self, user_id: str, provider: str) -> Optional[IndexingJob]:
        return None

    async def list_jobs(self, user_id: str, limit: int = 10) -> List[IndexingJob]:
        return []

    async def get_stale_running_jobs(self, updated_before: datetime) -> List[IndexingJob]:
        return []
