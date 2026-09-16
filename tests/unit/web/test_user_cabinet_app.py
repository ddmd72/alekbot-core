"""
Unit tests for the /api/gmail/index cabinet route (async Cloud Tasks path).

Covers: the job must be persisted to Firestore before the Cloud Task is
enqueued. Cloud Tasks can dispatch to /worker in well under 100ms — faster
than the reverse order commits — and the worker's load_job_for_execution()
404s on "not found" if the task races ahead of the write (see
alek_debug.log incident 2026-08-31, job 5696f822: enqueue-then-persist
order produced a real 404, rescued only by Cloud Tasks' automatic retry).
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.email import IndexingJob
from src.web.user_cabinet_app import create_user_cabinet_blueprint

_USER_ID = "user-1"
_ACCOUNT_ID = "account-1"


def _job(job_id="job-1"):
    now = datetime.now(timezone.utc)
    return IndexingJob(
        job_id=job_id,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        provider="gmail",
        triggered_by="cabinet",
        status="running",
        started_at=now,
        updated_at=now,
    )


def _app(*, email_indexing_service, email_job_repo, task_queue, oauth_credentials_port):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(
        return_value={"sub": _USER_ID, "account_id": _ACCOUNT_ID, "role": "owner"}
    )
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(),
        session_service=session_service,
        user_repo=MagicMock(),
        fact_repo=MagicMock(),
        embedding_service=MagicMock(),
        oauth_credentials_port=oauth_credentials_port,
        email_indexing_service=email_indexing_service,
        email_job_repo=email_job_repo,
        task_queue=task_queue,
    )
    app = Quart("test_app")
    app.register_blueprint(bp)
    return app


class TestGmailIndexAsyncPath:

    async def test_persists_job_before_enqueueing_task(self):
        job = _job("job-1")
        call_order = []

        email_indexing_service = MagicMock()
        email_indexing_service.create_job = MagicMock(return_value=job)

        email_job_repo = MagicMock()
        email_job_repo.create_job = AsyncMock(side_effect=lambda j: call_order.append("persist"))

        task_queue = MagicMock()
        task_queue.enqueue_email_indexing_task = AsyncMock(
            side_effect=lambda job_id: call_order.append("enqueue")
        )

        oauth_credentials_port = MagicMock()
        oauth_credentials_port.get_credentials = AsyncMock(return_value=MagicMock())

        app = _app(
            email_indexing_service=email_indexing_service,
            email_job_repo=email_job_repo,
            task_queue=task_queue,
            oauth_credentials_port=oauth_credentials_port,
        )

        async with app.test_client() as client:
            resp = await client.post(
                "/api/gmail/index",
                headers={"Authorization": "Bearer token"},
                json={"mode": "incremental"},
            )

        assert resp.status_code == 202
        body = await resp.get_json()
        assert body["job_id"] == "job-1"

        # The regression this test guards: enqueue must never precede persist,
        # since Cloud Tasks can dispatch to /worker before the return here.
        assert call_order == ["persist", "enqueue"]
        email_job_repo.create_job.assert_awaited_once_with(job)
        task_queue.enqueue_email_indexing_task.assert_awaited_once_with("job-1")
