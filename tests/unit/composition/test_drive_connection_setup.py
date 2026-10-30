"""The drive activation of a completed consent and the connection wiring (MULTI_DRIVE_RFC §4.4, §5.2; slice 1b P1b-1):
the grant's drive identity is read through the drive port (a probe handle for a new grant, dropped with `forget`; the
connection's own handle for a reconnect), the connection is created or reconnected, and a connection that indexes gets a
`drive_sync` task. The real `DriveConnectionService` over the in-memory store and the real credential store; the drive
is a stand-in answering `get_root` per handle; the queue is the real `GcpTaskQueue` over the recording client."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List

import pytest

from src.adapters.firestore_drive_connection_repo import FirestoreDriveConnectionRepository
from src.composition.drive_connection_setup import (
    build_drive_activation, build_drive_connection_repository, build_drive_connection_service, probe_connection_id,
)
from src.composition.oauth_connect_setup import build_connect_activations
from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_handle import DriveHandle
from src.domain.integration import Integration
from src.domain.provider_account import ProviderAccount
from src.domain.user_drive import DriveItem
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.recording_task_queue import make_task_queue

USER, KIND = "usr-1", "microsoft_onedrive"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Drive:
    """A drive port stand-in: `get_root` answers the identity scripted per credential (a missing one raises, as an
    unreachable provider would); every handle seen and every `forget` is recorded."""

    def __init__(self) -> None:
        self.identity: Dict[str, str] = {}
        self.roots: List[DriveHandle] = []
        self.forgotten: List[DriveHandle] = []
        self.forget_fails = False

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        self.roots.append(handle)
        if handle.credential_id not in self.identity:
            raise ConnectionError("provider unreachable")
        return DriveItem(item_id="root", name="", path="", is_folder=True,
                         drive_identity=self.identity[handle.credential_id])

    async def forget(self, handle: DriveHandle) -> None:
        self.forgotten.append(handle)
        if self.forget_fails:
            raise RuntimeError("cache trouble")


class World:
    def __init__(self, queue: bool = True) -> None:
        self.store = CredentialsStore()
        self.repo = FakeDriveConnectionRepository()
        self.connections = DriveConnectionService(self.repo, self.store.repo, clock=lambda: T0)
        self.drive = Drive()
        self.queue, self.tasks = make_task_queue() if queue else (None, None)
        self.activate = build_drive_activation(connections=self.connections, drive=self.drive,  # type: ignore[arg-type]
                                               task_queue=self.queue, clock=lambda: T0)

    async def consent(self, subject="subject-a"):
        credential = creds(USER, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"))
        return replace(credential, credential_id=await self.store.repo.save(credential))

    def drive_tasks(self):
        return [body for task_type, body in self.tasks.worker_tasks() if task_type == "drive_sync"]


class TestNewGrant:
    async def test_a_new_grant_is_read_through_a_probe_and_becomes_a_connection(self):
        w = World()
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(credential)
        (conn,) = w.repo.connections.values()
        assert (conn.credential_id, conn.drive_identity, conn.state) == (
            credential.credential_id, "microsoft_onedrive:drive-a", ConnectionState.ACTIVE)
        probe = DriveHandle(user_id=USER, connection_id=probe_connection_id(credential.credential_id),
                            credential_id=credential.credential_id, kind=KIND)
        assert w.drive.roots == [probe] and w.drive.forgotten == [probe]
        assert probe.connection_id != conn.connection_id
        assert w.drive_tasks() == []                         # a new connection does not index yet

    async def test_an_unreadable_identity_creates_nothing_and_raises(self, caplog):
        w = World()
        credential = await w.consent()
        with pytest.raises(RuntimeError):
            await w.activate(credential)
        assert w.repo.connections == {}
        assert len(w.drive.forgotten) == 1
        assert "could not be read" in caplog.text

    async def test_a_failed_forget_of_the_probe_does_not_stop_the_connect(self):
        w = World()
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        w.drive.forget_fails = True
        await w.activate(credential)
        assert len(w.repo.connections) == 1

    async def test_a_new_grant_of_a_known_drive_reconnects_its_connection(self):
        w = World()
        first = await w.consent()
        w.drive.identity[first.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(first)
        await w.store.repo.delete(USER, first.credential_id)
        second = await w.consent()
        w.drive.identity[second.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(second)
        (conn,) = w.repo.connections.values()
        assert conn.credential_id == second.credential_id


class TestReconnect:
    async def test_a_reconnect_reads_through_the_connection_handle_and_resumes_its_index(self):
        w = World()
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(credential)
        (conn,) = w.repo.connections.values()
        await w.connections.set_indexing(USER, conn.connection_id, True)
        await w.connections.flag_reconnect(conn.handle)
        w.drive.roots.clear()
        w.drive.forgotten.clear()
        await w.activate(await w.consent())                  # the same account keeps its credential id
        assert w.drive.roots == [conn.handle] and w.drive.forgotten == []
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.ACTIVE
        (task,) = w.drive_tasks()
        assert (task["user_id"], task["connection_id"]) == (USER, conn.connection_id)

    async def test_an_unreadable_identity_on_reconnect_keeps_the_stored_one(self):
        w = World()
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(credential)
        (conn,) = w.repo.connections.values()
        await w.connections.flag_reconnect(conn.handle)
        del w.drive.identity[credential.credential_id]
        await w.activate(credential)
        stored = w.repo.connections[(USER, conn.connection_id)]
        assert (stored.state, stored.drive_identity) == (ConnectionState.ACTIVE, "microsoft_onedrive:drive-a")

    async def test_without_a_queue_an_indexing_reconnect_still_connects(self):
        w = World(queue=False)
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(credential)
        (conn,) = w.repo.connections.values()
        await w.connections.set_indexing(USER, conn.connection_id, True)
        await w.activate(credential)
        assert w.repo.connections[(USER, conn.connection_id)].state is ConnectionState.ACTIVE

    async def test_a_failed_enqueue_is_logged_and_the_connect_counts(self, caplog):
        w = World()
        credential = await w.consent()
        w.drive.identity[credential.credential_id] = "microsoft_onedrive:drive-a"
        await w.activate(credential)
        (conn,) = w.repo.connections.values()
        await w.connections.set_indexing(USER, conn.connection_id, True)

        async def refused(*args, **kwargs):
            raise RuntimeError("queue down")
        w.queue.enqueue_worker_task = refused
        await w.activate(credential)
        assert "drive sync after a connect failed" in caplog.text


class TestWiring:
    async def test_the_connect_activations_run_the_drive_activation(self):
        seen = []

        async def drive_activation(credential):
            seen.append(credential)
        activations = build_connect_activations(reconnect=None, todo_connector=ConnectorRegistry().get("microsoft_todo"),  # type: ignore[arg-type]
                                                task_queue=None, drive_activation=drive_activation)
        credential = creds(USER, KIND)
        await activations[Integration.DRIVE](credential)
        assert seen == [credential]

    async def test_with_no_drive_wired_a_drive_consent_only_logs(self, caplog):
        activations = build_connect_activations(reconnect=None, todo_connector=ConnectorRegistry().get("microsoft_todo"),  # type: ignore[arg-type]
                                                task_queue=None, drive_activation=None)
        await activations[Integration.DRIVE](creds(USER, KIND))
        assert "no drive wired" in caplog.text

    def test_the_repository_and_service_use_the_two_collections(self):
        env = SimpleNamespace(drive_connections_collection="eval_drive_connections",
                              drive_user_settings_collection="eval_drive_user_settings")
        db = EvalDb()
        repo = build_drive_connection_repository(db, env)
        assert isinstance(repo, FirestoreDriveConnectionRepository)
        assert (repo._col.name, repo._settings.name) == ("eval_drive_connections", "eval_drive_user_settings")
        assert isinstance(build_drive_connection_service(db, env, credentials=CredentialsStore().repo),  # type: ignore[arg-type]
                          DriveConnectionService)

    def test_the_drive_kind_takes_several_connections(self):
        assert ConnectorRegistry().get(KIND).multiple is True
