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
                      credential_revocation=self.revocation, user_drive_service=self.router, drive_search_service=Search(),
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


# ---------------------------------------------------------------------------------------------- indexing switch


# ---------------------------------------------------------------------------------------------- disconnect

class TestDisconnect:


    async def test_a_remaining_drive_keeps_the_deep_research_copy(self):
        w = World(copy_on=True)
        await w.connect(subject="subject-a", identity="microsoft_onedrive:drive-a")
        await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
        await call(w.app(), "delete", "/api/drive/disconnect")
        assert w.users.profiles[USER].config.drive_copy_deep_research is True
        assert w.users.updates == 0


