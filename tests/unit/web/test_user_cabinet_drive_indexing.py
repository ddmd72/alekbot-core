"""Drive indexing in the Cabinet: status, toggle, task on toggle (USER_DRIVE_INDEXING_RFC §6, §7.6; RFC Δ2)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.drive_task_keys import DRIVE_SYNC_TASK
from src.domain.user import UserBotConfig
from src.domain.user_drive import DriveConnection
from src.ports.task_queue import TaskQueue
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_search_service import DriveSearchService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_AUTH = {"Authorization": "Bearer token"}
NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def _drive(connected=True, needs_reconnect=False):
    drive = MagicMock(spec=UserDrivePort)
    drive.connection = AsyncMock(return_value=DriveConnection(connected=connected, needs_reconnect=needs_reconnect,
                                                              drive_id="d1"))
    drive.display_name = "OneDrive"
    return drive


def _users(enabled=False):
    repo = MagicMock()
    profile = SimpleNamespace(config=SimpleNamespace(drive_indexing_enabled=enabled))
    repo.get_user = AsyncMock(return_value=profile)
    repo.update_user = AsyncMock()
    return repo, profile


def _app(user_drive=None, search=None, queue=None, user_repo=None):
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": "u1", "account_id": "a1", "role": "owner"})
    app = Quart("test_drive_indexing")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=user_repo or MagicMock(),
        fact_repo=MagicMock(), embedding_service=MagicMock(), user_drive=user_drive,
        drive_search_service=search, drive_task_queue=queue, connectors=ConnectorRegistry(),
    ))
    return app


def _search(status):
    search = AsyncMock(spec=DriveSearchService)
    search.indexing_status.return_value = status
    return search


class TestStatus:
    async def test_connected_with_the_index_block(self):
        status = DriveIndexingStatus(enabled=True, counters={"metadata_only": 40, "carded": 39},
                                     last_success_at=NOW, last_error="throttled", index_as_of=NOW,
                                     index_full=False, building=True,
                                     readable_chunks=17, usd_month={"reader": 0.5, "triage": 0.25},
                                     usd_caps={"reader": 4.0, "triage": 3.0}, initial_build_spent_usd=40.0,
                                     initial_build_allowance_usd=40.0, initial_build_closed=True)
        # §27.12/G24: the status reads the user's own transcription flag; a real config with the flag at its default
        users = MagicMock()
        users.get_user = AsyncMock(return_value=SimpleNamespace(config=UserBotConfig()))
        async with _app(_drive(), _search(status), user_repo=users).test_client() as client:
            body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
        assert body == {
            "connected": True, "provider": "OneDrive", "needs_reconnect": False,
            "indexing": {"enabled": True, "counters": {"metadata_only": 40, "carded": 39},
                         "last_success_at": NOW.isoformat(), "last_error": "throttled",
                         "index_as_of": NOW.isoformat(), "index_full": False, "building": True,
                         "available": False, "access_lost_at": None, "index_kept": True,
                         "awaiting_rebuild": False,     # XC-8: a v1 index waiting for its v2 rebuild
                         # review rv_sv2_p5 MINOR 3 (spec change, §25.4.5): dollars per class against the month's
                         # cap and the initial build replace the retired §24.7 page and image counters
                         "readable_chunks": 17, "model_usd_month": {"reader": 0.5, "triage": 0.25},
                         "model_usd_caps": {"reader": 4.0, "triage": 3.0},
                         "initial_build": {"spent_usd": 40.0, "allowance_usd": 40.0, "closed": True},
                         "transcribe_enabled": False,    # §27.12/G24: the user's flag, always present
                         "mirror_root": False},          # Δ202: the user has a _Transcripts folder, always present
        }

    async def test_needs_reconnect(self):
        status = DriveIndexingStatus(enabled=True, counters={})
        async with _app(_drive(needs_reconnect=True), _search(status)).test_client() as client:
            body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
        assert body["needs_reconnect"] is True and body["connected"] is True

    async def test_disconnected_never_needs_reconnect(self):
        async with _app(_drive(connected=False, needs_reconnect=True)).test_client() as client:
            body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
        assert body == {"connected": False, "provider": "OneDrive", "needs_reconnect": False}

    async def test_without_indexing_wired_there_is_no_block(self):
        async with _app(_drive()).test_client() as client:
            body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
        assert "indexing" not in body

    async def test_failure_is_500_not_a_leak(self):
        drive = _drive()
        drive.connection.side_effect = RuntimeError("token=secret")
        async with _app(drive).test_client() as client:
            resp = await client.get("/api/drive/status", headers=_AUTH)
        assert resp.status_code == 500 and "secret" not in (await resp.get_data(as_text=True))


class TestToggle:
    async def test_get(self):
        repo, _ = _users(enabled=True)
        async with _app(user_repo=repo).test_client() as client:
            body = await (await client.get("/api/drive/indexing", headers=_AUTH)).get_json()
        assert body == {"enabled": True}

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_put_saves_the_flag_and_enqueues_a_toggle_tick(self, enabled):
        repo, profile = _users(enabled=not enabled)
        queue = AsyncMock(spec=TaskQueue)
        async with _app(user_repo=repo, queue=queue).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": enabled})
        assert resp.status_code == 200 and (await resp.get_json()) == {"enabled": enabled, "queued": True}
        assert profile.config.drive_indexing_enabled is enabled
        repo.update_user.assert_awaited_once_with(profile)
        call = queue.enqueue_worker_task.await_args
        assert call.args == (DRIVE_SYNC_TASK, {"user_id": "u1"})
        assert call.kwargs["deadline_seconds"] == TICK_DEADLINE_S
        assert call.kwargs["dedup_key"].startswith("u1:toggle:")

    @pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}])
    async def test_put_needs_a_bool(self, body):
        repo, _ = _users()
        async with _app(user_repo=repo).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json=body)
        assert resp.status_code == 400
        repo.update_user.assert_not_called()

    async def test_flag_saved_even_when_the_queue_fails(self):
        repo, profile = _users()
        queue = AsyncMock(spec=TaskQueue)
        queue.enqueue_worker_task.side_effect = RuntimeError("queue down")
        async with _app(user_repo=repo, queue=queue).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert resp.status_code == 200 and (await resp.get_json())["queued"] is False
        assert profile.config.drive_indexing_enabled is True

    async def test_without_a_queue(self):
        repo, _ = _users()
        async with _app(user_repo=repo).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert (await resp.get_json()) == {"enabled": True, "queued": False}

    async def test_unknown_user(self):
        repo = MagicMock()
        repo.get_user = AsyncMock(return_value=None)
        async with _app(user_repo=repo).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert resp.status_code == 404
