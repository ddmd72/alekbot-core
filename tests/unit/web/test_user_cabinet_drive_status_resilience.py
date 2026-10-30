"""The Cabinet drive card stays usable whatever the index does (USER_DRIVE_INDEXING_RFC §6, §7.7; RFC Δ2).

- The connection card (Connect/Disconnect/Reconnect) never depends on the index read.
- The index block is shown whether or not the drive is connected.
- Each toggle gets its own dedup key.
- The flag is saved before the toggle tick is enqueued, and saved with no queue at all.
- With no drive queue, the log does not claim a scheduler will run.
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.user_drive import DriveConnection
from src.ports.task_queue import TaskQueue
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_search_service import DriveSearchService
from src.web import user_cabinet_app
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from src.composition.connector_registry import ConnectorRegistry

_AUTH = {"Authorization": "Bearer token"}


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
    app = Quart("test_drive_status_resilience")
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


class TestStatusResilience:
    async def test_status_survives_an_indexing_read_failure(self, caplog):
        """A Firestore blip, a building index or an unparsable sync state hides only the index block."""
        search = _search(None)
        search.indexing_status.side_effect = RuntimeError("index building")
        with caplog.at_level(logging.ERROR):
            async with _app(_drive(needs_reconnect=True), search).test_client() as client:
                resp = await client.get("/api/drive/status", headers=_AUTH)
        assert resp.status_code == 200
        body = await resp.get_json()
        assert body == {"connected": True, "provider": "OneDrive", "needs_reconnect": True}
        assert any(r.levelno == logging.ERROR and "indexing status" in r.getMessage() for r in caplog.records)

    async def test_disconnected_still_carries_the_index_block(self):
        status = DriveIndexingStatus(enabled=True, counters={})
        async with _app(_drive(connected=False), _search(status)).test_client() as client:
            body = await (await client.get("/api/drive/status", headers=_AUTH)).get_json()
        assert body["connected"] is False and body["indexing"]["enabled"] is True


class TestToggleOrdering:
    async def test_two_toggles_get_two_distinct_dedup_keys(self, monkeypatch):
        """Cloud Tasks reserves a task name for up to a day: a reused key drops the second toggle."""
        ticks = iter([datetime(2026, 10, 12, 9, tzinfo=timezone.utc) + timedelta(seconds=i) for i in range(2)])

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(ticks)

        # Two requests can land in the same millisecond; the test pins two distinct toggle moments.
        monkeypatch.setattr(user_cabinet_app, "datetime", _Clock)
        repo, _ = _users()
        queue = AsyncMock(spec=TaskQueue)
        async with _app(user_repo=repo, queue=queue).test_client() as client:
            await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
            await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": False})
        keys = [c.kwargs["dedup_key"] for c in queue.enqueue_worker_task.await_args_list]
        assert len(keys) == 2 and keys[0] != keys[1]
        assert all(k.startswith("u1:toggle:") and len(k) > len("u1:toggle:") for k in keys)

    async def test_flag_is_saved_before_the_toggle_tick_is_enqueued(self):
        """The tick's gate reads the flag (§7.1 step 0): it must find the new value."""
        repo, _ = _users()
        queue = AsyncMock(spec=TaskQueue)
        order = MagicMock()
        order.attach_mock(repo.update_user, "update_user")
        order.attach_mock(queue.enqueue_worker_task, "enqueue_worker_task")
        async with _app(user_repo=repo, queue=queue).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert resp.status_code == 200
        names = [c[0] for c in order.mock_calls if c[0] in ("update_user", "enqueue_worker_task")]
        assert names == ["update_user", "enqueue_worker_task"]

    async def test_flag_is_saved_with_no_queue(self):
        repo, profile = _users()
        async with _app(user_repo=repo).test_client() as client:
            resp = await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        assert (await resp.get_json()) == {"enabled": True, "queued": False}
        repo.update_user.assert_awaited_once_with(profile)
        assert profile.config.drive_indexing_enabled is True


class TestNoQueueLog:
    async def test_no_queue_log_does_not_promise_a_scheduler(self, caplog):
        """main.py builds the scheduler fan-out only with a drive queue: with none, nothing will run."""
        repo, _ = _users()
        with caplog.at_level(logging.WARNING):
            async with _app(user_repo=repo).test_client() as client:
                await client.put("/api/drive/indexing", headers=_AUTH, json={"enabled": True})
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("no drive queue" in m for m in warnings)
        assert not any("scheduler will pick it up" in m for m in warnings)
        assert any("no sync task will run" in m for m in warnings)
