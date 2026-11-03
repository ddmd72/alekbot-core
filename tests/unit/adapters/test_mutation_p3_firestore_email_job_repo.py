"""Mutation survivor of `src/adapters/firestore_email_job_repo.py` (p3 · g6): `get_latest_job` orders by started_at
DESC. Job ids are chosen so that the document-id order (what an unordered query returns) is the opposite of the
wanted one."""
from datetime import timedelta
from types import SimpleNamespace

from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.domain.email import IndexingJob
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.firestore_eval import EvalDb

JOBS = "eval_email_indexing_jobs_v1"
T = UTC_MIDNIGHT.utc


def _job(job_id, started):
    return IndexingJob(job_id=job_id, user_id="usr-1", account_id="acc-1", provider="gmail", triggered_by="cabinet",
                       status="running", started_at=started, updated_at=started)


async def test_get_latest_job_is_the_newest_by_started_at_not_by_id():
    repo = FirestoreEmailJobRepository(EvalDb(), SimpleNamespace(email_indexing_jobs_collection=JOBS))
    await repo.create_job(_job("a-oldest", T - timedelta(hours=3)))
    await repo.create_job(_job("m-newest", T - timedelta(hours=1)))
    await repo.create_job(_job("z-middle", T - timedelta(hours=2)))
    assert (await repo.get_latest_job("usr-1", "gmail")).job_id == "m-newest"
