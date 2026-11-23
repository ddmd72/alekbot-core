"""
Unit tests for the /api/gmail/index cabinet route (async Cloud Tasks path).

Covers: the job must be persisted to Firestore before the Cloud Task is
enqueued. Cloud Tasks can dispatch to /worker in well under 100ms — faster
than the reverse order commits — and the worker's load_job_for_execution()
404s on "not found" if the task races ahead of the write (see
alek_debug.log incident 2026-08-31, job 5696f822: enqueue-then-persist
order produced a real 404, rescued only by Cloud Tasks' automatic retry).
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.email import IndexingJob
from src.web.user_cabinet_app import CABINET_ASSETS, create_user_cabinet_blueprint
from tests.support.cabinet_session_kit import cabinet_session_for
from src.composition.connector_registry import ConnectorRegistry
from tests.support.oauth_credentials_store import mirror_get

_AUTH: dict = {}   # the Bearer header of the app's one signed-in session, set by `_app()`

_STATIC = Path(__file__).resolve().parents[3] / "src" / "web" / "static"
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
    cabinet_sessions, _bearer = cabinet_session_for(_USER_ID, _ACCOUNT_ID)
    _AUTH.update(_bearer)
    bp = create_user_cabinet_blueprint(
        invite_service=MagicMock(),
        cabinet_sessions=cabinet_sessions,
        user_repo=MagicMock(),
        fact_repo=MagicMock(),
        embedding_service=MagicMock(),
        oauth_credentials_port=oauth_credentials_port,
        email_indexing_service=email_indexing_service,
        email_job_repo=email_job_repo,
        task_queue=task_queue, connectors=ConnectorRegistry(),
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
        oauth_credentials_port.get = AsyncMock(return_value=MagicMock())
        mirror_get(oauth_credentials_port)

        app = _app(
            email_indexing_service=email_indexing_service,
            email_job_repo=email_job_repo,
            task_queue=task_queue,
            oauth_credentials_port=oauth_credentials_port,
        )

        async with app.test_client() as client:
            resp = await client.post(
                "/api/gmail/index",
                headers=_AUTH,
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


class TestCabinetPage:

    async def test_cabinet_page_is_served_no_cache(self):
        app = _app(email_indexing_service=MagicMock(), email_job_repo=MagicMock(),
                   task_queue=MagicMock(), oauth_credentials_port=MagicMock())
        resp = await app.test_client().get("/cabinet")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-cache"


class TestCabinetAssets:
    """/cabinet/assets/<name> serves the Cabinet page's own files from an allowlist (CABINET_REDESIGN_RFC §6)."""

    @staticmethod
    def _client():
        return _app(email_indexing_service=MagicMock(), email_job_repo=MagicMock(),
                    task_queue=MagicMock(), oauth_credentials_port=MagicMock()).test_client()

    @pytest.mark.parametrize("name", sorted(CABINET_ASSETS))
    async def test_an_allowlisted_asset_is_served_with_its_type_and_revalidated(self, name):
        resp = await self._client().get(f"/cabinet/assets/{name}")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].split(";")[0] == CABINET_ASSETS[name]
        assert resp.headers["Cache-Control"] == "no-cache"
        assert await resp.get_data() == (_STATIC / name).read_bytes()

    def test_the_allowlist_is_exactly_what_the_shell_loads(self):
        shell = (_STATIC / "cabinet.html").read_text(encoding="utf-8")
        assert set(re.findall(r'(?:href|src)="/cabinet/assets/([^"]+)"', shell)) == set(CABINET_ASSETS)

    @pytest.mark.parametrize("path", [
        "/cabinet/assets/cabinet.html",            # a static file, but not an asset of the page
        "/cabinet/assets/session.js",
        "/cabinet/assets/call.html",
        "/cabinet/assets/unknown.js",
        "/cabinet/assets/..%2Fuser_cabinet_app.py",
        "/cabinet/assets/%2E%2E%2Fuser_cabinet_app.py",
        "/cabinet/assets/../user_cabinet_app.py",
        "/cabinet/assets/static/cabinet.css",
    ])
    async def test_anything_else_is_not_found(self, path):
        resp = await self._client().get(path)
        assert resp.status_code == 404
