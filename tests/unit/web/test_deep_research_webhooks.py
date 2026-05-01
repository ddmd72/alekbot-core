"""
Unit tests for the OpenAI Deep Research webhook blueprint.

Covers the production-class regression fixed in R11.1 (channel propagation)
and the symmetry gap closed by R12.3 (media_storage wiring).

Tests assert that handle_openai_deep_research:
  - extracts channel_id from per-channel session_id ("user:channel" format)
  - forwards channel_id_override + media_storage to deliver_deep_research
  - falls back to channel_id_override=None on legacy session_ids without colon
  - delegates failure / cancellation events to notification_service.notify
"""

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from quart import Quart

from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint


_USER = "user-abc"
_ACCOUNT = "acc-xyz"
_CHANNEL = "C09SLACK"


def _make_app(notification_service, media_storage=None, task_queue=None):
    app = Quart("test_app")
    app.register_blueprint(
        create_deep_research_webhooks_blueprint(
            notification_service=notification_service,
            webhook_secret=None,  # disables signature verification in dev mode
            media_storage=media_storage,
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


class TestOpenAIWebhookChannelPropagation:
    """R11.1 — channel_id_override must be derived from session_id."""

    async def test_per_channel_session_id_extracts_channel(self):
        notification = AsyncMock()
        media_storage = MagicMock()
        task_queue = AsyncMock()

        app = _make_app(notification, media_storage=media_storage, task_queue=task_queue)
        payload = _completed_payload(session_id=f"{_USER}:{_CHANNEL}")

        with patch(
            "src.web.deep_research_webhooks.deliver_deep_research",
            new_callable=AsyncMock,
        ) as mock_deliver:
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )

            assert resp.status_code == 200
            mock_deliver.assert_called_once()
            kwargs = mock_deliver.call_args.kwargs
            assert kwargs["channel_id_override"] == _CHANNEL
            assert kwargs["media_storage"] is media_storage
            assert kwargs["task_queue"] is task_queue
            assert kwargs["user_id"] == _USER
            assert kwargs["account_id"] == _ACCOUNT
            assert kwargs["session_id"] == f"{_USER}:{_CHANNEL}"

    async def test_legacy_session_id_without_colon_yields_none(self):
        """Legacy session_id (pre-per-channel-sessions) must not crash; channel=None."""
        notification = AsyncMock()
        app = _make_app(notification)
        payload = _completed_payload(session_id=_USER)  # no colon

        with patch(
            "src.web.deep_research_webhooks.deliver_deep_research",
            new_callable=AsyncMock,
        ) as mock_deliver:
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )

            assert resp.status_code == 200
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args.kwargs["channel_id_override"] is None

    async def test_empty_session_id_yields_none(self):
        """Empty session_id (no metadata) must not crash; channel=None."""
        notification = AsyncMock()
        app = _make_app(notification)
        payload = _completed_payload(session_id="")

        with patch(
            "src.web.deep_research_webhooks.deliver_deep_research",
            new_callable=AsyncMock,
        ) as mock_deliver:
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )

            assert resp.status_code == 200
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args.kwargs["channel_id_override"] is None

    async def test_output_text_fallback_to_output_array(self):
        """When response_obj.output_text is empty, extract from output[].content[] (Responses API format)."""
        notification = AsyncMock()
        app = _make_app(notification)
        payload = {
            "type": "response.completed",
            "data": {
                "id": "resp_123",
                "output_text": "",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Fallback extracted text."}
                        ],
                    }
                ],
                "metadata": {
                    "user_id": _USER,
                    "account_id": _ACCOUNT,
                    "query": "Q",
                    "session_id": f"{_USER}:{_CHANNEL}",
                },
            },
        }

        with patch(
            "src.web.deep_research_webhooks.deliver_deep_research",
            new_callable=AsyncMock,
        ) as mock_deliver:
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhooks/openai/deep-research",
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                )

            assert resp.status_code == 200
            mock_deliver.assert_called_once()
            assert mock_deliver.call_args.kwargs["result_text"] == "Fallback extracted text."


class TestFactoryWiring:
    """Factory contract — main.py:572 wiring guarantees."""

    def test_factory_accepts_media_storage_kwarg(self):
        """main.py passes media_storage=gcs_media_adapter; factory must accept it without TypeError."""
        notification = AsyncMock()
        media_storage = MagicMock()
        bp = create_deep_research_webhooks_blueprint(
            notification_service=notification,
            webhook_secret=None,
            media_storage=media_storage,
            task_queue=None,
        )
        assert bp.name == "deep_research_webhooks"

    def test_factory_accepts_all_none_optionals(self):
        """All optional kwargs default to None — must not raise during construction."""
        bp = create_deep_research_webhooks_blueprint(notification_service=AsyncMock())
        assert bp.name == "deep_research_webhooks"

    def test_main_py_wires_media_storage_to_factory(self):
        """main.py:572 must pass media_storage= to create_deep_research_webhooks_blueprint.

        Catches regression where someone refactors the call site and silently
        drops the media_storage kwarg, re-introducing R12.3 (round files lost
        for OpenAI DR path)."""
        repo_root = Path(__file__).resolve().parents[3]
        main_py = repo_root / "main.py"
        tree = ast.parse(main_py.read_text())

        kwargs_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else None
                )
                if func_name == "create_deep_research_webhooks_blueprint":
                    kwargs_names = [kw.arg for kw in node.keywords]
                    break

        assert kwargs_names, (
            "create_deep_research_webhooks_blueprint(...) call not found in main.py"
        )
        assert "media_storage" in kwargs_names, (
            f"main.py must pass media_storage= to create_deep_research_webhooks_blueprint; "
            f"got kwargs={kwargs_names}"
        )
        assert "notification_service" in kwargs_names
        assert "task_queue" in kwargs_names


class TestOpenAIWebhookFailureEvents:
    """response.failed and response.cancelled delegate to notification_service.notify."""

    async def test_response_failed_notifies_user(self):
        notification = AsyncMock()
        app = _make_app(notification)
        payload = {
            "type": "response.failed",
            "data": {
                "id": "resp_123",
                "error": {"message": "boom"},
                "metadata": {"user_id": _USER, "account_id": _ACCOUNT},
            },
        }

        async with app.test_client() as client:
            resp = await client.post(
                "/webhooks/openai/deep-research",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        notification.notify.assert_called_once()

    async def test_response_cancelled_notifies_user(self):
        notification = AsyncMock()
        app = _make_app(notification)
        payload = {
            "type": "response.cancelled",
            "data": {
                "id": "resp_123",
                "metadata": {"user_id": _USER, "account_id": _ACCOUNT},
            },
        }

        async with app.test_client() as client:
            resp = await client.post(
                "/webhooks/openai/deep-research",
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        notification.notify.assert_called_once()
