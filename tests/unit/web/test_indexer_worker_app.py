"""Indexer service routes: drive_sync routing and the /worker OIDC gate
(USER_DRIVE_INDEXING_RFC §9 "Host", §12.5)."""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.domain.drive_index import Nudge
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_task_keys import scope_payload
from src.services.drive_indexing_service import TickResult
from src.web import indexer_worker_app
from src.web.indexer_worker_app import create_indexer_worker_blueprint, indexer_audiences, worker_oidc_gate


def _app(tick_result=None, verifier=None, raises=None):
    indexing = AsyncMock()
    indexing.tick.return_value = tick_result or TickResult("done", {"observed": 3})
    if raises is not None:
        indexing.tick.side_effect = raises
    oidc = verifier or AsyncMock(return_value=True)
    app = Quart(__name__)
    app.register_blueprint(create_indexer_worker_blueprint(indexing, oidc))
    return app, indexing, oidc


SCOPE = ConnectionScope("u1", "c1")


def _task(scope=SCOPE, nudge=None):
    return {"task_type": "drive_sync", **scope_payload(scope, nudge)}


class TestRouting:
    async def test_drive_sync_runs_the_tick(self):
        app, indexing, _ = _app()
        resp = await app.test_client().post("/worker", json=_task(), headers={"Authorization": "Bearer t"})
        assert resp.status_code == 200
        assert await resp.get_json() == {"status": "done", "observed": 3}
        indexing.tick.assert_awaited_once_with(SCOPE, None)   # per connection (MULTI_DRIVE_RFC §8)

    async def test_nudge_is_parsed_and_passed(self):
        app, indexing, _ = _app()
        nudge = Nudge(mutation_id="m1", item_ids=("a", "b"), attempt=2)
        await app.test_client().post("/worker", json=_task(nudge=nudge))
        indexing.tick.assert_awaited_once_with(SCOPE, nudge)

    @pytest.mark.parametrize("status", ["disabled", "busy", "purging", "purged", "access_lost", "throttled",
                                        "error"])
    async def test_every_handled_outcome_is_200(self, status):
        app, _, _ = _app(TickResult(status))
        resp = await app.test_client().post("/worker", json=_task())
        assert resp.status_code == 200 and (await resp.get_json())["status"] == status

    async def test_unknown_task_type_is_400(self):
        app, indexing, _ = _app()
        resp = await app.test_client().post("/worker", json={"task_type": "consolidation", "user_id": "u1"})
        assert resp.status_code == 400
        indexing.tick.assert_not_called()

    async def test_missing_user_id_is_400(self):
        app, indexing, _ = _app()
        resp = await app.test_client().post("/worker", json={"task_type": "drive_sync"})
        assert resp.status_code == 400
        indexing.tick.assert_not_called()

    async def test_unhandled_exception_is_500_for_the_queue_retry(self):
        app, _, _ = _app(raises=RuntimeError("firestore down"))
        resp = await app.test_client().post("/worker", json=_task())
        assert resp.status_code == 500

    async def test_unconfigured_indexer_is_503(self):
        app = Quart(__name__)
        app.register_blueprint(create_indexer_worker_blueprint(None, AsyncMock(return_value=True)))
        resp = await app.test_client().post("/worker", json=_task())
        assert resp.status_code == 503
        assert (await app.test_client().get("/health")).status_code == 200

    async def test_cloud_tasks_delivery_is_logged(self, caplog):
        app, _, _ = _app()
        await app.test_client().post("/worker", json=_task(), headers={
            "X-CloudTasks-TaskName": "drive_sync-abc", "X-CloudTasks-TaskRetryCount": "1",
            "X-CloudTasks-TaskExecutionCount": "2"})
        assert "/worker delivery: task_type=drive_sync task=drive_sync-abc retry_count=1 execution_count=2" \
            in caplog.text

    async def test_health(self):
        app, _, _ = _app()
        resp = await app.test_client().get("/health")
        assert resp.status_code == 200


class TestOidcGate:
    async def test_rejected_token_is_401_and_no_tick(self):
        app, indexing, oidc = _app(verifier=AsyncMock(return_value=False))
        resp = await app.test_client().post("/worker", json=_task(), headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 401
        oidc.assert_awaited_once_with("Bearer bad")
        indexing.tick.assert_not_called()

    async def test_gate_open_without_service_account_in_local_dev(self, monkeypatch):
        verify = AsyncMock()
        monkeypatch.setattr(indexer_worker_app, "verify_worker_oidc", verify)
        # The gate takes the accepted set (IAM least-privilege, Step 0 C1); "no identity" is an empty set.
        assert await worker_oidc_gate(frozenset())(None) is True
        assert await worker_oidc_gate(set())("Bearer x") is True
        verify.assert_not_called()

    async def test_gate_fails_closed_on_cloud_run_without_service_account(self, monkeypatch, caplog):
        """A dropped secret must not open /worker (RFC Δ21)."""
        verify = AsyncMock()
        monkeypatch.setattr(indexer_worker_app, "verify_worker_oidc", verify)
        gate = worker_oidc_gate(frozenset(), ("https://indexer.example.invalid",), on_cloud_run=True)
        assert await gate("Bearer anything") is False
        verify.assert_not_called()
        assert "refuses every call" in caplog.text

    async def test_gate_enforced_with_service_account_and_pinned_audience(self, monkeypatch):
        calls = []

        def fake_verify(header, sa, audiences):
            calls.append((header, sa, audiences))
            return header == "Bearer good"

        monkeypatch.setattr(indexer_worker_app, "verify_worker_oidc", fake_verify)
        audiences = indexer_audiences("https://indexer.example.invalid")
        accepted = frozenset({"sa@example.invalid"})
        gate = worker_oidc_gate(accepted, audiences, on_cloud_run=True)
        assert await gate("Bearer good") is True
        assert await gate(None) is False
        assert calls == [("Bearer good", accepted, audiences),
                         (None, accepted, audiences)]

    def test_audiences_are_the_service_url_and_its_worker_route(self):
        assert indexer_audiences(" https://indexer.example.invalid/ ") == (
            "https://indexer.example.invalid", "https://indexer.example.invalid/worker")
        assert indexer_audiences(None) is None and indexer_audiences("") is None
