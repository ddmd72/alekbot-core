"""The v1 `reindex` mode is refused (email index v2 §6.4, card slice 1): it re-saves whole documents, which would wipe
every card. Pinned through the real `EmailIndexingService` over the real `FirestoreEmailJobRepository` on the
evaluating Firestore fake; the mailbox, index, classifier and embedding ports are absent, so any call to them fails
the test."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.composition.connector_registry import ConnectorRegistry
from src.domain.email import IndexingJob
from src.services import email_indexing_service as module
from src.services.email_indexing_service import REINDEX_DISABLED, EmailIndexingService
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.firestore_eval import EvalDb
from tests.unit.services.test_email_indexing_service import _make_credentials

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
JOBS = "eval_email_indexing_jobs_v1"
STARTED = UTC_MIDNIGHT.utc - timedelta(minutes=5)


class _Lines:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def error(self, *a, **k): pass


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={JOBS: "development_email_indexing_jobs_v1"})
        self.jobs = FirestoreEmailJobRepository(self.db, SimpleNamespace(email_indexing_jobs_collection=JOBS))
        self.service = EmailIndexingService(None, None, self.jobs, None, None, None, None,
                                            connector=ConnectorRegistry().get("gmail"))

    @property
    def docs(self) -> dict:
        return self.db.collection(JOBS).docs

    async def job(self, **over) -> IndexingJob:
        fields = dict(job_id="j1", user_id="u1", account_id="a1", provider="gmail", triggered_by="cabinet",
                      status="running", mode="reindex", next_page_token="page-7", started_at=STARTED,
                      updated_at=STARTED)
        fields.update(over)
        job = IndexingJob(**fields)
        await self.jobs.create_job(job)
        return job


@pytest.fixture
def world():
    return World()


async def test_a_reindex_job_ends_failed_before_any_mailbox_or_index_call(world, monkeypatch):
    lines = _Lines()
    monkeypatch.setattr(module, "logger", lines)
    job = await world.job()

    got = await world.service.run_indexing_job(job, _make_credentials(), "a1", mode="reindex")

    assert got.status == "failed" and got.next_page_token is None      # the worker re-enqueues nothing
    stored = world.docs["j1"]
    assert stored["status"] == "failed" and stored["next_page_token"] is None
    assert stored["errors"] == [{"email_id": "", "stage": "mode", "error": REINDEX_DISABLED}]
    assert stored["completed_at"] is not None and stored["updated_at"] >= STARTED
    assert stored["emails_fetched"] == 0 and stored["emails_stored"] == 0
    assert any("[reindex] refused" in w for w in lines.warnings)


async def test_the_refusal_keeps_earlier_errors_capped_at_one_hundred(world):
    earlier = [{"email_id": f"m{i}", "stage": "classify", "error": "x"} for i in range(100)]
    job = await world.job(errors=earlier)

    await world.service.run_indexing_job(job, _make_credentials(), "a1", mode="reindex")

    stored = world.docs["j1"]["errors"]
    assert len(stored) == 100 and stored[-1]["error"] == REINDEX_DISABLED and stored[0]["email_id"] == "m1"


async def test_start_job_with_reindex_creates_the_job_and_refuses_it(world):
    got = await world.service.start_job("u1", "a1", _make_credentials(), triggered_by="cabinet", mode="reindex")

    assert got.status == "failed" and got.mode == "reindex"
    assert world.docs[got.job_id]["status"] == "failed"
