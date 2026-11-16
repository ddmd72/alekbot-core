"""POST /api/gmail/index modes: `reindex` is refused with 400 before any credential read, job or task; the other
modes keep working (email index v2: the re-index mode would erase the email cards)."""
from unittest.mock import AsyncMock, MagicMock

from src.services.email_indexing_service import REINDEX_DISABLED
from tests.support.oauth_credentials_store import mirror_get
from tests.unit.web.test_user_cabinet_app import _app, _job


class _Rig:
    def __init__(self):
        self.jobs = []
        self.enqueued = []
        self.create_job_modes = []

        service = MagicMock()

        def _create_job(**kwargs):
            self.create_job_modes.append((kwargs["mode"], kwargs.get("backfill_until")))
            return _job("job-1")

        service.create_job = MagicMock(side_effect=_create_job)
        repo = MagicMock()
        repo.create_job = AsyncMock(side_effect=lambda j: self.jobs.append(j))
        queue = MagicMock()
        queue.enqueue_email_indexing_task = AsyncMock(side_effect=lambda job_id: self.enqueued.append(job_id))
        creds = MagicMock()
        creds.get = AsyncMock(return_value=MagicMock())
        mirror_get(creds)
        self.creds = creds
        self.app = _app(email_indexing_service=service, email_job_repo=repo, task_queue=queue,
                        oauth_credentials_port=creds)

    async def post(self, body):
        async with self.app.test_client() as client:
            resp = await client.post("/api/gmail/index", headers={"Authorization": "Bearer token"}, json=body)
            return resp.status_code, await resp.get_json()


async def test_reindex_is_refused_before_any_job_or_task():
    rig = _Rig()
    status, body = await rig.post({"mode": "reindex"})
    assert status == 400
    assert body["error"] == REINDEX_DISABLED == "reindex_disabled"
    assert "detail" in body
    assert rig.jobs == [] and rig.enqueued == [] and rig.create_job_modes == []
    rig.creds.get.assert_not_called()


async def test_incremental_creates_and_enqueues_a_job():
    rig = _Rig()
    status, body = await rig.post({"mode": "incremental"})
    assert status == 202 and body["job_id"] == "job-1"
    assert len(rig.jobs) == 1 and rig.enqueued == ["job-1"]
    assert rig.create_job_modes == [("incremental", None)]


async def test_backfill_with_a_date_creates_and_enqueues_a_job():
    rig = _Rig()
    status, _ = await rig.post({"mode": "backfill", "backfill_until": "2026-01-15"})
    assert status == 202
    assert len(rig.jobs) == 1 and rig.enqueued == ["job-1"]
    assert rig.create_job_modes[0][0] == "backfill" and rig.create_job_modes[0][1] is not None


async def test_unknown_mode_is_a_400_naming_the_valid_modes():
    rig = _Rig()
    status, body = await rig.post({"mode": "everything"})
    assert status == 400
    assert "incremental" in body["error"] and "backfill" in body["error"]
    assert rig.jobs == [] and rig.enqueued == []
