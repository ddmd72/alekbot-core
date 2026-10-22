"""
Unit tests for the OpenAI Deep Research webhook and the worker task that delivers its outcome.

Covers the production-class regression fixed in R11.1 (channel propagation) and the symmetry gap
closed by R12.3 (media_storage wiring). Since S1b task W the webhook only verifies, filters,
retrieves, claims and enqueues `deep_research_webhook_event`; WorkerHandler delivers. So:
  - webhook side: signature fail-closed, failure events enqueued, factory/main.py wiring
  - worker side: channel_id from a per-channel session_id ("user:channel"), media_storage and
    task_queue reaching deliver_deep_research, failure / cancellation events notifying the user
"""

import ast
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

from src.handlers.worker_handler import WorkerHandler
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint


_USER = "user-abc"
_ACCOUNT = "acc-xyz"
_CHANNEL = "C09SLACK"


def _make_app(task_queue=None):
    app = Quart("test_app")
    app.register_blueprint(
        create_deep_research_webhooks_blueprint(
            webhook_secret=None,  # disables signature verification in dev mode
            task_queue=task_queue,
        )
    )
    return app


def _completed_payload(session_id: str) -> dict:
    return {
        "type": "response.completed",
        "data": {
            "id": "resp_123",
            "output_text": "Research findings text.",
            "metadata": {
                "user_id": _USER,
                "account_id": _ACCOUNT,
                "query": "What is X?",
                "session_id": session_id,
            },
        },
    }


def _worker(media_storage=None, task_dispatch=None, notification=None):
    notification = notification or AsyncMock()
    notification.notify.return_value = MagicMock(delivered=True, sent=True, error=None)
    worker = WorkerHandler(
        agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
        notification_service=notification, consolidation_service=None,
        coordinator=MagicMock(), agent_factory=AsyncMock(),
        indexed_email_repo=None, user_repo=MagicMock(),
        task_dispatch=task_dispatch or AsyncMock(), media_storage=media_storage,
        deep_research_fetch=AsyncMock(return_value={"output_text": "Research findings text.", "model": "m"}),
    )
    return worker, notification


def _event(event_type="response.completed", session_id=f"{_USER}:{_CHANNEL}") -> dict:
    return {"task_type": "deep_research_webhook_event", "event_type": event_type, "job_id": "resp_123",
            "user_id": _USER, "account_id": _ACCOUNT, "session_id": session_id,
            "origin_platform": "", "query": "What is X?"}


class TestOpenAIWebhookChannelPropagation:
    """R11.1 — channel_id_override must be derived from session_id (now in the worker task)."""

    async def test_per_channel_session_id_extracts_channel(self):
        media_storage = MagicMock()
        task_dispatch = AsyncMock()
        worker, _ = _worker(media_storage=media_storage, task_dispatch=task_dispatch)

        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as mock_deliver:
            _, status = await worker._dispatch(_event(), {})

        assert status == 200
        mock_deliver.assert_called_once()
        kwargs = mock_deliver.call_args.kwargs
        assert kwargs["channel_id_override"] == _CHANNEL
        assert kwargs["media_storage"] is media_storage
        assert kwargs["task_queue"] is task_dispatch
        assert kwargs["user_id"] == _USER
        assert kwargs["account_id"] == _ACCOUNT
        assert kwargs["session_id"] == f"{_USER}:{_CHANNEL}"

    async def test_webhook_forwards_session_id_to_the_worker_task(self):
        task_queue = AsyncMock()
        app = _make_app(task_queue=task_queue)
        async with app.test_client() as client:
            resp = await client.post(
                "/webhooks/openai/deep-research",
                data=json.dumps(_completed_payload(session_id=f"{_USER}:{_CHANNEL}")),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        payload = task_queue.enqueue_worker_task.await_args.kwargs["payload"]
        assert payload["session_id"] == f"{_USER}:{_CHANNEL}"
        assert payload["user_id"] == _USER and payload["account_id"] == _ACCOUNT

    async def test_legacy_session_id_without_colon_yields_none(self):
        """Legacy session_id (pre-per-channel-sessions) must not crash; channel=None."""
        worker, _ = _worker()
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as mock_deliver:
            _, status = await worker._dispatch(_event(session_id=_USER), {})  # no colon
        assert status == 200
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.kwargs["channel_id_override"] is None

    async def test_empty_session_id_yields_none(self):
        """Empty session_id (no metadata) must not crash; channel=None."""
        worker, _ = _worker()
        with patch("src.handlers.worker_handler.deliver_deep_research",
                   new_callable=AsyncMock, return_value=True) as mock_deliver:
            _, status = await worker._dispatch(_event(session_id=""), {})
        assert status == 200
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.kwargs["channel_id_override"] is None

    # test_output_text_fallback_to_output_array removed (S1b W): the worker re-retrieves by job id and
    # OpenAIDeepResearchAdapter.fetch_response returns the SDK's Response.output_text, which itself
    # aggregates output[].content[] output_text items; no `output` array reaches the delivery any more.


def _call_kwargs(func_name: str) -> list:
    tree = ast.parse((Path(__file__).resolve().parents[3] / "main.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name == func_name:
                return [kw.arg for kw in node.keywords]
    return []


class TestFactoryWiring:
    """Factory contract and main.py wiring guarantees."""

    def test_factory_accepts_all_none_optionals(self):
        """All optional kwargs default to None — must not raise during construction."""
        bp = create_deep_research_webhooks_blueprint()
        assert bp.name == "deep_research_webhooks"

    @pytest.mark.datafile
    def test_main_py_wires_media_storage_to_the_delivering_worker(self):
        """R12.3 (round files lost for the OpenAI DR path): media_storage must reach the OpenAI
        delivery. Delivery now runs in WorkerHandler, so main.py must pass media_storage= (and the
        link service, task queue and notification service delivery uses) to WorkerHandler, and the
        webhook factory must still get its task queue and retrieval callable."""
        worker_kwargs = _call_kwargs("WorkerHandler")
        assert worker_kwargs, "WorkerHandler(...) call not found in main.py"
        for kw in ("media_storage", "link_service", "task_dispatch", "notification_service",
                   "deep_research_fetch"):
            assert kw in worker_kwargs, f"main.py must pass {kw}= to WorkerHandler; got {worker_kwargs}"

        factory_kwargs = _call_kwargs("create_deep_research_webhooks_blueprint")
        assert factory_kwargs, "create_deep_research_webhooks_blueprint(...) call not found in main.py"
        for kw in ("task_queue", "fetch_response", "dedup_store", "webhook_secret"):
            assert kw in factory_kwargs, f"main.py must pass {kw}= to the webhook factory; got {factory_kwargs}"


class TestOpenAIWebhookFailClosed:
    """Hardening: a missing webhook_secret must reject in deployed environments."""

    async def test_no_secret_fails_closed_when_deployed(self):
        task_queue = AsyncMock()
        app = _make_app(task_queue=task_queue)  # webhook_secret=None
        payload = _completed_payload(session_id=f"{_USER}:{_CHANNEL}")
        with patch.dict(os.environ, {"K_SERVICE": "alek-bot-dev"}):
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )
            assert resp.status_code == 401
            task_queue.enqueue_worker_task.assert_not_called()

    async def test_no_secret_allows_processing_locally(self):
        task_queue = AsyncMock()
        app = _make_app(task_queue=task_queue)  # webhook_secret=None
        payload = _completed_payload(session_id=f"{_USER}:{_CHANNEL}")
        env = dict(os.environ)
        env.pop("K_SERVICE", None)
        with patch.dict(os.environ, env, clear=True):
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )
            assert resp.status_code == 200
            task_queue.enqueue_worker_task.assert_called_once()


class TestOpenAIWebhookFailureEvents:
    """response.failed and response.cancelled reach notification_service.notify (via the worker task)."""

    @pytest.mark.slow
    @pytest.mark.parametrize("event", ["response.failed", "response.cancelled"])
    async def test_failure_event_is_enqueued_for_the_worker(self, event):
        task_queue = AsyncMock()
        app = _make_app(task_queue=task_queue)
        payload = {"type": event, "data": {
            "id": "resp_123", "error": {"message": "boom"},
            "metadata": {"user_id": _USER, "account_id": _ACCOUNT}}}
        async with app.test_client() as client:
            resp = await client.post(
                "/webhooks/openai/deep-research",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 200
        task_queue.enqueue_worker_task.assert_awaited_once()
        kw = task_queue.enqueue_worker_task.await_args.kwargs
        assert kw["task_type"] == "deep_research_webhook_event"
        assert kw["payload"]["event_type"] == event

    async def test_response_failed_notifies_user(self):
        worker, notification = _worker()
        _, status = await worker._dispatch(_event("response.failed"), {})
        assert status == 200
        notification.notify.assert_called_once()

    async def test_response_cancelled_notifies_user(self):
        worker, notification = _worker()
        _, status = await worker._dispatch(_event("response.cancelled"), {})
        assert status == 200
        notification.notify.assert_called_once()
