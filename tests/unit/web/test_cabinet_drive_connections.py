"""The Cabinet's drive card on drive connections (MULTI_DRIVE_RFC §4.3, §4.4, §8; slice 1b P1b-1, P1b-9): status, the
indexing switch and disconnect act on one connection (the primary, else the oldest live one) until stage 2's list;
the switches are the connection's, a live connection without its grant reads Reconnect (R1), the `drive_sync` task is
per connection, and a disconnect ends the connection, removes its grant under the revoke rule, drops the adapter's
caches, enqueues the purge and turns the deep research copy off when no primary is left.

The real `DriveConnectionService` over the in-memory connection store, the real credential store on `EvalDb`, the real
revocation service and R1 service, the real `GcpTaskQueue` over the recording Cloud Tasks client; assertions on what
is stored and what was enqueued."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_handle import DriveHandle
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.provider_account import ProviderAccount
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.recording_task_queue import make_task_queue

USER = "usr-1"
AUTH = {"Authorization": "Bearer token"}
KIND = "microsoft_onedrive"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Users:
    """A user repository with the two calls the drive routes make; `profiles` is what is stored."""

    def __init__(self, copy_on: bool) -> None:
        self.profiles: Dict[str, Any] = {USER: SimpleNamespace(config=SimpleNamespace(drive_copy_deep_research=copy_on))}
        self.updates = 0
        self.fail_update = False

    async def get_user(self, user_id: str) -> Any:
        return self.profiles.get(user_id)

    async def update_user(self, profile: Any) -> None:
        if self.fail_update:
            raise RuntimeError("store down")
        self.updates += 1


class Router:
    """The router's one call the Cabinet makes: `forget` of a disconnected connection."""

    def __init__(self) -> None:
        self.forgotten: List[DriveHandle] = []

    async def forget(self, handle: DriveHandle) -> None:
        self.forgotten.append(handle)


class Search:
    async def indexing_status(self, user_id: str) -> DriveIndexingStatus:
        return DriveIndexingStatus(enabled=False, counters={})

    async def has_mirror_root(self, user_id: str) -> bool:
        return False


class World:
    def __init__(self, *, copy_on: bool = False, queue: bool = True, switch_on: bool = True) -> None:
        self.registry = ConnectorRegistry()
        self.store = CredentialsStore()
        self.repo = FakeDriveConnectionRepository()
        tags = iter(["a1", "b2", "c3", "d4"])
        ids = iter(["conn-1", "conn-2", "conn-3", "conn-4"])
        self.connections = DriveConnectionService(self.repo, self.store.repo, clock=lambda: T0,
                                                  new_id=lambda: next(ids), new_tag=lambda: next(tags))
        self.users = Users(copy_on)
        self.router = Router()
        self.queue, self.tasks = make_task_queue() if queue else (None, None)
        self.revocation = CredentialRevocationService(self.store.repo, self.registry.edges())
        self.reconnect = IntegrationReconnectService(
            credentials=self.store.repo, users=MagicMock(), notifier=None, drive_connections=self.connections,
            mail_connector=self.registry.get("gmail"), todo_connector=self.registry.get("microsoft_todo"))
        self.switch_on = switch_on

    async def connect(self, subject="subject-a", identity="microsoft_onedrive:drive-a"):
        credential = creds(USER, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"))
        saved = replace(credential, credential_id=await self.store.repo.save(credential))
        return await self.connections.activate(saved, identity)

    def app(self, **over) -> Quart:
        session_service = MagicMock()
        session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": "acc-1",
                                                                      "role": "owner"})
        kwargs = dict(invite_service=MagicMock(), session_service=session_service, user_repo=self.users,
                      fact_repo=MagicMock(), embedding_service=MagicMock(), oauth_credentials_port=self.store.repo,
                      credential_revocation=self.revocation, user_drive=self.router, drive_search_service=Search(),
                      drive_task_queue=self.queue, drive_transcribe_switch_on=self.switch_on, reconnect=self.reconnect,
                      connectors=self.registry, drive_connections=self.connections)
        kwargs.update(over)
        app = Quart("test_cabinet_drive_connections")
        app.register_blueprint(create_user_cabinet_blueprint(**kwargs))
        return app

    def drive_tasks(self) -> List[Dict[str, Any]]:
        return [body for task_type, body in self.tasks.worker_tasks() if task_type == "drive_sync"]


async def call(app: Quart, method: str, path: str, body: Any = None):
    async with app.test_client() as client:
        resp = await getattr(client, method)(path, headers=AUTH, json=body) if body is not None else \
            await getattr(client, method)(path, headers=AUTH)
        return resp.status_code, await resp.get_json()


# ---------------------------------------------------------------------------------------------- status

class TestStatus:
    async def test_no_connection_reads_not_connected_with_the_provider_label(self):
        w = World()
        status, body = await call(w.app(), "get", "/api/drive/status")
        assert status == 200
        assert (body["connected"], body["provider"], body["needs_reconnect"], body["reconnect_needed"]) == (
            False, "OneDrive", False, False)
        assert "credential_id" not in body

    async def test_a_connected_drive_names_its_credential(self):
        w = World()
        conn = await w.connect()
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert (body["connected"], body["provider"], body["needs_reconnect"]) == (True, "OneDrive", False)
        assert body["credential_id"] == conn.credential_id
        assert "reconnect_needed" not in body

    async def test_a_flagged_drive_reads_connected_needs_reconnect(self):
        w = World()
        conn = await w.connect()
        await w.connections.flag_reconnect(conn.handle)
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert (body["connected"], body["needs_reconnect"]) == (True, True)

    async def test_a_live_connection_without_its_grant_reads_reconnect(self):
        w = World()
        conn = await w.connect()
        await w.store.repo.delete(USER, conn.credential_id)
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert (body["connected"], body["needs_reconnect"], body["reconnect_needed"]) == (False, False, True)
        assert "credential_id" not in body

    async def test_a_disconnected_drive_never_reads_reconnect(self):
        w = World()
        conn = await w.connect()
        await w.connections.disconnect(USER, conn.connection_id)
        await w.store.repo.delete(USER, conn.credential_id)
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert (body["connected"], body["reconnect_needed"]) == (False, False)

    @pytest.mark.parametrize("switch_on, flag, shown", [(True, True, True), (True, False, False),
                                                         (False, True, False)])
    async def test_the_transcription_disclosure_needs_the_connection_switch_and_the_kill_switch(
            self, switch_on, flag, shown):
        w = World(switch_on=switch_on)
        conn = await w.connect()
        await w.connections.set_transcribe(USER, conn.connection_id, flag)
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert body["indexing"]["transcribe_enabled"] is shown

    async def test_without_the_connection_service_the_card_reads_not_connected(self):
        w = World()
        status, body = await call(w.app(drive_connections=None), "get", "/api/drive/status")
        assert (status, body) == (200, {"connected": False, "provider": ""})

    async def test_a_store_failure_is_500(self):
        w = World()

        async def broken(user_id):
            raise RuntimeError("store down")
        w.repo.get_settings = broken
        status, body = await call(w.app(), "get", "/api/drive/status")
        assert (status, body) == (500, {"error": "Internal server error"})


# ---------------------------------------------------------------------------------------------- indexing switch

class TestIndexingSwitch:
    async def test_get_reads_the_card_connection(self):
        w = World()
        assert (await call(w.app(), "get", "/api/drive/indexing"))[1] == {"enabled": False}
        conn = await w.connect()
        await w.connections.set_indexing(USER, conn.connection_id, True)
        assert (await call(w.app(), "get", "/api/drive/indexing"))[1] == {"enabled": True}

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_put_stores_the_switch_on_the_primary_and_enqueues_its_connection(self, enabled):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        if not enabled:
            await w.connections.set_indexing(USER, first.connection_id, True)
        status, body = await call(w.app(), "put", "/api/drive/indexing", {"enabled": enabled})
        assert (status, body) == (200, {"enabled": enabled, "queued": True})
        assert w.repo.connections[(USER, first.connection_id)].indexing_enabled is enabled
        assert w.repo.connections[(USER, second.connection_id)].indexing_enabled is False
        (task,) = w.drive_tasks()
        assert (task["user_id"], task["connection_id"]) == (USER, first.connection_id)

    async def test_two_toggles_enqueue_two_tasks(self):
        w = World()
        await w.connect()
        await call(w.app(), "put", "/api/drive/indexing", {"enabled": True})
        await call(w.app(), "put", "/api/drive/indexing", {"enabled": False})
        assert len(w.drive_tasks()) == 2

    @pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}])
    async def test_put_needs_a_bool(self, body):
        w = World()
        conn = await w.connect()
        status, _ = await call(w.app(), "put", "/api/drive/indexing", body)
        assert status == 400
        assert w.repo.connections[(USER, conn.connection_id)].indexing_enabled is False
        assert w.drive_tasks() == []

    async def test_put_without_a_drive_is_404(self):
        w = World()
        status, body = await call(w.app(), "put", "/api/drive/indexing", {"enabled": True})
        assert (status, body) == (404, {"error": "Drive not connected"})
        assert w.drive_tasks() == []

    async def test_put_without_the_connection_service_is_501(self):
        status, _ = await call(World().app(drive_connections=None), "put", "/api/drive/indexing", {"enabled": True})
        assert status == 501

    async def test_without_a_queue_the_switch_is_saved_and_nothing_is_queued(self, caplog):
        w = World(queue=False)
        conn = await w.connect()
        status, body = await call(w.app(), "put", "/api/drive/indexing", {"enabled": True})
        assert (status, body) == (200, {"enabled": True, "queued": False})
        assert w.repo.connections[(USER, conn.connection_id)].indexing_enabled is True
        assert "no drive queue wired" in caplog.text

    async def test_a_failed_enqueue_keeps_the_switch(self, caplog):
        w = World()
        conn = await w.connect()

        async def refused(*args, **kwargs):
            raise RuntimeError("queue down")
        w.queue.enqueue_worker_task = refused
        status, body = await call(w.app(), "put", "/api/drive/indexing", {"enabled": True})
        assert (status, body) == (200, {"enabled": True, "queued": False})
        assert w.repo.connections[(USER, conn.connection_id)].indexing_enabled is True
        assert "sync task was not queued" in caplog.text

    async def test_a_store_failure_is_500(self):
        w = World()
        await w.connect()

        async def broken(*args, **kwargs):
            raise RuntimeError("store down")
        w.repo.update = broken
        status, _ = await call(w.app(), "put", "/api/drive/indexing", {"enabled": True})
        assert status == 500
        assert w.drive_tasks() == []


# ---------------------------------------------------------------------------------------------- disconnect

class TestDisconnect:
    async def test_a_disconnect_ends_the_connection_removes_its_grant_and_enqueues_its_purge(self):
        w = World()
        conn = await w.connect()
        status, body = await call(w.app(), "delete", "/api/drive/disconnect")
        assert (status, body) == (200, {"success": True})
        stored = w.repo.connections[(USER, conn.connection_id)]
        assert (stored.state, stored.ref_tag) == (ConnectionState.DISCONNECTED, conn.ref_tag)
        assert await w.store.repo.list_keys(USER, KIND) == []
        assert w.router.forgotten == [conn.handle]
        (task,) = w.drive_tasks()
        assert (task["user_id"], task["connection_id"]) == (USER, conn.connection_id)
        assert w.repo.settings[USER].primary_connection_id is None

    async def test_only_the_card_connection_is_disconnected_and_the_primary_passes_on(self):
        w = World()
        first = await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await call(w.app(), "delete", "/api/drive/disconnect")
        assert w.repo.connections[(USER, first.connection_id)].state is ConnectionState.DISCONNECTED
        assert w.repo.connections[(USER, second.connection_id)].state is ConnectionState.ACTIVE
        assert [k.credential_id for k in await w.store.repo.list_keys(USER, KIND)] == [second.credential_id]
        assert w.repo.settings[USER].primary_connection_id == second.connection_id

    async def test_the_last_drive_turns_the_deep_research_copy_off(self):
        w = World(copy_on=True)
        await w.connect()
        await call(w.app(), "delete", "/api/drive/disconnect")
        assert w.users.profiles[USER].config.drive_copy_deep_research is False
        assert w.users.updates == 1

    async def test_a_remaining_drive_keeps_the_deep_research_copy(self):
        w = World(copy_on=True)
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await call(w.app(), "delete", "/api/drive/disconnect")
        assert w.users.profiles[USER].config.drive_copy_deep_research is True
        assert w.users.updates == 0

    async def test_a_failed_copy_reset_does_not_fail_the_disconnect(self):
        w = World(copy_on=True)
        conn = await w.connect()
        w.users.fail_update = True
        status, _ = await call(w.app(), "delete", "/api/drive/disconnect")
        assert status == 200
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.DISCONNECTED

    async def test_a_flagged_drive_can_be_disconnected(self):
        w = World()
        conn = await w.connect()
        await w.connections.flag_reconnect(conn.handle)
        await call(w.app(), "delete", "/api/drive/disconnect")
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.DISCONNECTED
        assert await w.store.repo.list_keys(USER, KIND) == []

    async def test_after_a_disconnect_the_status_reads_not_connected_without_reconnect(self):
        w = World()
        await w.connect()
        await call(w.app(), "delete", "/api/drive/disconnect")
        _, body = await call(w.app(), "get", "/api/drive/status")
        assert (body["connected"], body["reconnect_needed"]) == (False, False)

    async def test_no_connection_is_nothing_to_do(self):
        w = World()
        status, body = await call(w.app(), "delete", "/api/drive/disconnect")
        assert (status, body) == (200, {"success": True})
        assert w.drive_tasks() == [] and w.router.forgotten == []

    @pytest.mark.parametrize("missing", ["drive_connections", "credential_revocation"])
    async def test_without_the_services_it_is_501(self, missing):
        w = World()
        conn = await w.connect()
        status, _ = await call(w.app(**{missing: None}), "delete", "/api/drive/disconnect")
        assert status == 501
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.ACTIVE

    async def test_a_failed_grant_removal_is_500_after_the_connection_ended(self):
        w = World()
        conn = await w.connect()

        async def broken(user_id, credential_id):
            raise RuntimeError("store down")
        w.revocation.remove = broken
        status, _ = await call(w.app(), "delete", "/api/drive/disconnect")
        assert status == 500
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.DISCONNECTED

    async def test_a_failed_forget_does_not_fail_the_disconnect(self, caplog):
        w = World()
        await w.connect()

        async def broken(handle):
            raise RuntimeError("cache trouble")
        w.router.forget = broken
        status, _ = await call(w.app(), "delete", "/api/drive/disconnect")
        assert status == 200
        assert len(w.drive_tasks()) == 1
        assert "caches failed" in caplog.text
