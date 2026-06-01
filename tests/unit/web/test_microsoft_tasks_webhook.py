"""
Unit tests for the Microsoft Tasks webhook blueprint — clientState verification.

Focus: the fail-closed hardening. When no webhook_secret is configured, an
unverifiable Graph notification must be rejected in a deployed environment
(K_SERVICE set), and only tolerated on a local laptop run.
"""
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from quart import Quart

from src.web.microsoft_tasks_webhook import create_microsoft_tasks_webhook_blueprint


def _make_app(webhook_secret, task_indexing=None, task_setup=None):
    task_indexing = task_indexing or AsyncMock()
    task_setup = task_setup or AsyncMock()
    app = Quart("test_app")
    app.register_blueprint(
        create_microsoft_tasks_webhook_blueprint(
            task_indexing=task_indexing,
            task_setup=task_setup,
            webhook_secret=webhook_secret,
        )
    )
    return app, task_indexing, task_setup


def _notification(client_state="anything"):
    return {
        "value": [
            {
                "subscriptionId": "sub12345",
                "clientState": client_state,
                "resource": "lists('L1')/tasks",
                "changeType": "updated",
            }
        ]
    }


class TestClientStateVerification:

    @pytest.mark.asyncio
    async def test_no_secret_fails_closed_when_deployed(self):
        app, _, task_setup = _make_app(webhook_secret=None)
        with patch.dict(os.environ, {"K_SERVICE": "alek-bot-dev"}):
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhook/microsoft-tasks/user-abc",
                    data=json.dumps(_notification()),
                    headers={"Content-Type": "application/json"},
                )
        # Notification rejected before any reindex side-effect.
        assert resp.status_code == 202  # Graph always gets 202
        task_setup.enqueue_reindex_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_secret_allows_processing_locally(self):
        # No K_SERVICE → local dev → unverified notification is processed.
        app, _, task_setup = _make_app(webhook_secret=None)
        env = dict(os.environ)
        env.pop("K_SERVICE", None)
        with patch.dict(os.environ, env, clear=True):
            async with app.test_client() as client:
                resp = await client.post(
                    "/webhook/microsoft-tasks/user-abc",
                    data=json.dumps(_notification()),
                    headers={"Content-Type": "application/json"},
                )
        assert resp.status_code == 202
        task_setup.enqueue_reindex_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_mismatched_client_state_skips_processing(self):
        app, _, task_setup = _make_app(webhook_secret="secret123")
        async with app.test_client() as client:
            resp = await client.post(
                "/webhook/microsoft-tasks/user-abc",
                data=json.dumps(_notification(client_state="wrong-secret")),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 202
        task_setup.enqueue_reindex_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_matching_client_state_processes(self):
        app, _, task_setup = _make_app(webhook_secret="secret123")
        async with app.test_client() as client:
            resp = await client.post(
                "/webhook/microsoft-tasks/user-abc",
                data=json.dumps(_notification(client_state="secret123")),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code == 202
        task_setup.enqueue_reindex_list.assert_called_once()

    @pytest.mark.asyncio
    async def test_validation_token_challenge_echoes(self):
        app, _, _ = _make_app(webhook_secret="secret123")
        async with app.test_client() as client:
            resp = await client.post(
                "/webhook/microsoft-tasks/user-abc?validationToken=ABC123",
            )
        assert resp.status_code == 200
        body = await resp.get_data()
        assert body == b"ABC123"
