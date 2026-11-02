"""The Cabinet's drive connection list and per-connection routes (MULTI_DRIVE_RFC §11.7-§11.9, slice 2b P-E).

The real `DriveConnectionService`, `UserDriveService` (over the fake drive adapter), `DriveDisconnectService` and
credential store; assertions on what is stored, enqueued and answered. Every route acts only on the caller's own
connections: another user's connection id answers 404 and changes nothing."""
import logging
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_connection import ROOT_FOLDER
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_indexing_status import DriveIndexingStatus
from src.domain.drive_mounts import HEIR_INVALID_LINE, PRIMARY_NEEDS_WRITE_FOLDER_LINE
from src.domain.provider_account import ProviderAccount
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService
from src.services.drive_disconnect_service import DriveDisconnectService
from src.services.integration_reconnect_service import IntegrationReconnectService
from src.services.user_drive_service import UserDriveService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.support.recording_task_queue import make_task_queue

USER, OTHER = "usr-1", "usr-2"
AUTH = {"Authorization": "Bearer token"}
KIND = "microsoft_onedrive"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Users:
    def __init__(self, copy_on: bool = False) -> None:
        self.profile = SimpleNamespace(config=SimpleNamespace(drive_copy_deep_research=copy_on))
        self.updates = 0
        self.fail_update = False

    async def get_user(self, user_id: str) -> Any:
        return self.profile

    async def update_user(self, profile: Any) -> None:
        if self.fail_update:
            raise RuntimeError("store down")
        self.updates += 1


class Search:
    def __init__(self) -> None:
        self.seen: List[Any] = []
        self.fail = False

    async def indexing_status(self, user_id: str, *, connection_id=None) -> DriveIndexingStatus:
        self.seen.append(connection_id)
        if self.fail:
            raise RuntimeError("index down")
        return DriveIndexingStatus(enabled=False, counters={}, usd_month={"reader": 0.12345}, usd_caps={"reader": 5.0},
                                   initial_build_spent_usd=1.5, initial_build_allowance_usd=40.0)

    async def has_mirror_root(self, user_id: str) -> bool:
        return True


class Notes:
    def __init__(self) -> None:
        self.texts: Dict[str, str] = {}

    async def drive_note_texts(self, user_id: str) -> Dict[str, str]:
        return self.texts


class World:
    def __init__(self, *, copy_on: bool = False, queue: bool = True) -> None:
        self.registry = ConnectorRegistry()
        self.store = CredentialsStore()
        self.repo = FakeDriveConnectionRepository()
        tags, ids = iter(["a1", "b2", "c3", "d4", "e5"]), iter(["conn-1", "conn-2", "conn-3", "conn-4", "conn-5"])
        self.connections = DriveConnectionService(self.repo, self.store.repo, clock=lambda: T0,
                                                  new_id=lambda: next(ids), new_tag=lambda: next(tags))
        self.adapter = FakeDriveAdapter()
        self.drive = UserDriveService(self.adapter, self.connections)
        self.users, self.search, self.notes = Users(copy_on), Search(), Notes()
        self.queue, self.tasks = make_task_queue() if queue else (None, None)
        self.revocation = CredentialRevocationService(self.store.repo, self.registry.edges())
        self.disconnect = DriveDisconnectService(self.connections, self.revocation, self.adapter, None, self.queue)
        self.reconnect = IntegrationReconnectService(
            credentials=self.store.repo, users=MagicMock(), notifier=None, drive_connections=self.connections,
            mail_connector=self.registry.get("gmail"), todo_connector=self.registry.get("microsoft_todo"))

    async def connect(self, user=USER, subject="subject-a", identity="microsoft_onedrive:drive-a", writable=True):
        credential = creds(user, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="Alex Work"))
        saved = replace(credential, credential_id=await self.store.repo.save(credential))
        connection = await self.connections.activate(saved, identity)
        if writable:
            capabilities = await self.adapter.capabilities(connection.handle)
            await self.connections.set_write_folder(user, connection.connection_id, ROOT_FOLDER,
                                                    capabilities=capabilities, ancestry=None)
        return connection

    async def folder(self, connection, name="R&D", parent=None) -> str:
        root = (await self.adapter.get_root(connection.handle)).item_id
        return self.adapter.plant(connection.handle, parent or root, name, folder=True)

    def app(self, **over) -> Quart:
        session_service = MagicMock()
        session_service.verify_access_token = MagicMock(return_value={"sub": USER, "account_id": "acc-1",
                                                                      "role": "owner"})
        kwargs = dict(invite_service=MagicMock(), session_service=session_service, user_repo=self.users,
                      fact_repo=MagicMock(), embedding_service=MagicMock(), oauth_credentials_port=self.store.repo,
                      credential_revocation=self.revocation, user_drive_service=self.drive,
                      drive_search_service=self.search, drive_task_queue=self.queue, drive_transcribe_switch_on=True,
                      reconnect=self.reconnect, connectors=self.registry, drive_connections=self.connections,
                      drive_notes=self.notes, drive_disconnect=self.disconnect)
        kwargs.update(over)
        app = Quart("test_cabinet_drive_connections_2b")
        app.register_blueprint(create_user_cabinet_blueprint(**kwargs))
        return app

    def drive_tasks(self) -> List[Dict[str, Any]]:
        return [body for task_type, body in self.tasks.worker_tasks() if task_type == "drive_sync"]


async def call(app: Quart, method: str, path: str, body: Any = None):
    async with app.test_client() as client:
        send = getattr(client, method)
        resp = await (send(path, headers=AUTH, json=body) if body is not None else send(path, headers=AUTH))
        return resp.status_code, await resp.get_json()


# ------------------------------------------------------------------------------------------------------ the list

async def test_the_list_describes_each_connection():
    w = World()
    first = await w.connect(writable=False)
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b", writable=False)
    folder = await w.folder(first, "R&D")
    capabilities = await w.adapter.capabilities(first.handle)
    chain = await w.drive.ancestry(first.handle, folder)
    await w.connections.set_scope(USER, first.connection_id, folder_ids=[folder], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={folder: chain})
    await w.connections.set_write_folder(USER, first.connection_id, folder, capabilities=capabilities, ancestry=chain)
    await w.connections.rename(USER, second.connection_id, "Acme")
    w.notes.texts = {second.connection_id: "Acme work files: R&D, Tom's plan"}
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200
    one, two = body["connections"]
    assert (one["connection_id"], one["mount_name"], one["kind"], one["provider_label"]) == (
        first.connection_id, "Drive", KIND, "OneDrive")
    assert (one["account_label"], one["state"], one["primary"]) == ("Alex Work", "active", True)
    assert one["write_folder"] == {"id": folder, "path": "R&D"}
    assert one["scope"] == {"whole_drive": False, "folders": [{"id": folder, "path": "R&D"}]}
    assert (one["searched_by_default"], one["indexing_enabled"], one["transcribe_enabled"]) == (True, False, False)
    assert one["reconnect"] is None and one["note"] == ""
    assert (two["mount_name"], two["primary"], two["write_folder"]) == ("Acme", False, None)
    assert two["note"] == "Acme work files: R&D, Tom's plan"
    assert body["default_name"]
    assert body["primary_connection_id"] == first.connection_id and body["mirror_root"] is True
    assert w.search.seen == [first.connection_id, second.connection_id]


async def test_the_index_block_carries_the_dollars_and_the_initial_build():
    w = World()
    await w.connect()
    block = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]["indexing"]
    assert block["model_usd_month"] == {"reader": 0.1235} and block["model_usd_caps"] == {"reader": 5.0}
    assert block["initial_build"] == {"spent_usd": 1.5, "allowance_usd": 40.0, "closed": False}
    assert block["available"] is True


async def test_available_says_whether_a_drive_queue_is_wired():
    w = World(queue=False)
    await w.connect()
    block = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]["indexing"]
    assert block["available"] is False


async def test_a_failing_index_read_leaves_the_row_without_a_block():
    w = World()
    await w.connect()
    w.search.fail = True
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200 and body["connections"][0]["indexing"] is None


async def test_transcription_shows_only_with_both_switches():
    w = World()
    conn = await w.connect()
    await w.connections.set_transcribe(USER, conn.connection_id, True)
    assert (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"][0]["transcribe_enabled"] is True
    assert (await call(w.app(drive_transcribe_switch_on=False), "get",
                       "/api/drive/connections"))[1]["connections"][0]["transcribe_enabled"] is False


async def test_a_flagged_connection_and_one_without_its_grant_carry_the_reconnect():
    w = World()
    flagged = await w.connect()
    lost = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    await w.connections.flag_reconnect(flagged.handle)
    await w.store.repo.delete(USER, lost.credential_id)
    rows = (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"]
    assert rows[0]["state"] == "needs_reconnect" and rows[0]["reconnect"] == {"credential_id": flagged.credential_id}
    assert rows[1]["reconnect"] == {"credential_id": lost.credential_id}


async def test_a_user_with_no_connection_gets_an_empty_list_and_the_default_name():
    w = World()
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200 and body["connections"] == [] and body["primary_connection_id"] is None
    assert body["default_name"] and body["reconnect_needed"] is False


async def test_a_disconnected_connection_is_not_listed_and_another_users_never():
    w = World()
    gone = await w.connect()
    await w.connect(user=OTHER, subject="subject-c", identity="microsoft_onedrive:drive-c")
    await w.connections.disconnect(USER, gone.connection_id)
    assert (await call(w.app(), "get", "/api/drive/connections"))[1]["connections"] == []


async def test_without_the_connection_service_the_routes_say_501():
    w = World()
    assert (await call(w.app(drive_connections=None), "get", "/api/drive/connections"))[0] == 501


# ------------------------------------------------------------------------------------------------------ rename

async def test_rename_stores_the_name_and_returns_it():
    w = World()
    conn = await w.connect()
    status, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/name", {"name": "Acme"})
    assert (status, body["name"]) == (200, "Acme") and "warning" in body
    assert (await w.connections.get(USER, conn.connection_id)).mount_name == "Acme"


async def test_rename_refusal_is_400_with_the_line_and_stores_nothing():
    w = World()
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    await w.connections.rename(USER, second.connection_id, "Acme")
    status, body = await call(w.app(), "put", f"/api/drive/connections/{first.connection_id}/name", {"name": "acme"})
    assert status == 400 and body["error"]
    assert (await w.connections.get(USER, first.connection_id)).mount_name == "Drive"


async def test_rename_needs_a_string_name():
    w = World()
    conn = await w.connect()
    assert (await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/name", {"name": 3}))[0] == 400


# ------------------------------------------------------------------------------------------------------ scope

async def test_scope_stores_the_folders_and_enqueues_the_toggle_task():
    w = World()
    conn = await w.connect(writable=False)
    folder = await w.folder(conn, "Coimbra 2024")
    status, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/scope",
                              {"whole_drive": False, "folder_ids": [folder]})
    assert (status, body["folder_ids"], body["queued"]) == (200, [folder], True)
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == (folder,)
    assert [t["connection_id"] for t in w.drive_tasks()] == [conn.connection_id]


async def test_scope_nested_folders_keep_the_outermost():
    w = World()
    conn = await w.connect(writable=False)
    outer = await w.folder(conn, "R&D")
    inner = await w.folder(conn, "Inbox", parent=outer)
    _, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/scope",
                         {"whole_drive": False, "folder_ids": [inner, outer]})
    assert body["folder_ids"] == [outer]


async def test_scope_both_is_400_and_a_scope_leaving_the_write_folder_is_400():
    w = World()
    conn = await w.connect()
    folder = await w.folder(conn)
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    status, body = await call(w.app(), "put", path, {"whole_drive": True, "folder_ids": [folder]})
    assert status == 400 and body["error"]
    # the write folder is the drive's root: a folders-only scope leaves it outside once the feed has folders
    status, body = await call(w.app(), "put", path, {"whole_drive": False, "folder_ids": [folder]})
    assert status == 400 and body["error"]
    assert (await w.connections.get(USER, conn.connection_id)).scope_folder_ids == ()
    assert w.drive_tasks() == []


async def test_scope_body_and_unknown_folder():
    w = World()
    conn = await w.connect()
    path = f"/api/drive/connections/{conn.connection_id}/scope"
    assert (await call(w.app(), "put", path, {"whole_drive": "yes"}))[0] == 400
    assert (await call(w.app(), "put", path, {"whole_drive": True, "folder_ids": ["nope"]}))[0] == 404


# ------------------------------------------------------------------------------------------------------ write folder

async def test_write_folder_set_to_a_folder_to_the_root_and_refused_while_primary_read_only():
    w = World()
    conn = await w.connect()
    folder = await w.folder(conn, "Inbox")
    path = f"/api/drive/connections/{conn.connection_id}/write-folder"
    capabilities = await w.adapter.capabilities(conn.handle)
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[], whole_drive=True, capabilities=capabilities,
                                  write_folder_ancestry=None, folder_ancestries={})
    status, body = await call(w.app(), "put", path, {"folder_id": folder})
    assert (status, body["write_folder"]) == (200, {"id": folder, "path": "Inbox"})
    assert (await call(w.app(), "put", path, {"folder_id": ""}))[1]["write_folder"] == {"id": "", "path": ""}
    status, body = await call(w.app(), "put", path, {"folder_id": None})
    assert (status, body["error"]) == (400, PRIMARY_NEEDS_WRITE_FOLDER_LINE)


async def test_write_folder_outside_the_scope_is_400_and_needs_the_field():
    w = World()
    conn = await w.connect(writable=False)
    scoped, other = await w.folder(conn, "R&D"), await w.folder(conn, "Inbox")
    capabilities = await w.adapter.capabilities(conn.handle)
    await w.connections.set_scope(USER, conn.connection_id, folder_ids=[scoped], whole_drive=False,
                                  capabilities=capabilities, write_folder_ancestry=None,
                                  folder_ancestries={scoped: await w.drive.ancestry(conn.handle, scoped)})
    path = f"/api/drive/connections/{conn.connection_id}/write-folder"
    status, body = await call(w.app(), "put", path, {"folder_id": other})
    assert status == 400 and body["error"]
    assert (await call(w.app(), "put", path, {}))[0] == 400


# ------------------------------------------------------------------------------------------------------ switches

async def test_searched_by_default_is_stored():
    w = World()
    conn = await w.connect()
    status, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/searched-by-default",
                              {"enabled": False})
    assert (status, body) == (200, {"enabled": False})
    assert (await w.connections.get(USER, conn.connection_id)).searched_by_default is False
    assert (await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/searched-by-default",
                       {"enabled": "no"}))[0] == 400


@pytest.mark.parametrize("enabled", [True, False])
async def test_indexing_stores_the_switch_of_that_connection_and_enqueues_it(enabled):
    w = World()
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    if not enabled:
        await w.connections.set_indexing(USER, second.connection_id, True)
    status, body = await call(w.app(), "put", f"/api/drive/connections/{second.connection_id}/indexing",
                              {"enabled": enabled})
    assert (status, body) == (200, {"enabled": enabled, "queued": True})
    assert (await w.connections.get(USER, second.connection_id)).indexing_enabled is enabled
    assert (await w.connections.get(USER, first.connection_id)).indexing_enabled is False
    assert [t["connection_id"] for t in w.drive_tasks()] == [second.connection_id]


async def test_two_toggles_enqueue_two_tasks():
    w = World()
    conn = await w.connect()
    path = f"/api/drive/connections/{conn.connection_id}/indexing"
    await call(w.app(), "put", path, {"enabled": True})
    await call(w.app(), "put", path, {"enabled": False})
    assert len(w.drive_tasks()) == 2


async def test_indexing_without_a_queue_saves_the_switch_and_queues_nothing():
    w = World(queue=False)
    conn = await w.connect()
    _, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing", {"enabled": True})
    assert body == {"enabled": True, "queued": False}
    assert (await w.connections.get(USER, conn.connection_id)).indexing_enabled is True


async def test_indexing_needs_a_bool():
    w = World()
    conn = await w.connect()
    assert (await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing",
                       {"enabled": 1}))[0] == 400


# ------------------------------------------------------------------------------------------------------ primary, folders

async def test_primary_moves_to_a_connection_with_a_write_folder_and_refuses_a_read_only_one():
    w = World()
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    read_only = await w.connect(subject="subject-c", identity="microsoft_onedrive:drive-c", writable=False)
    status, body = await call(w.app(), "put", "/api/drive/primary", {"connection_id": second.connection_id})
    assert (status, body) == (200, {"primary_connection_id": second.connection_id})
    assert (await w.connections.primary(USER)).connection_id == second.connection_id
    status, body = await call(w.app(), "put", "/api/drive/primary", {"connection_id": read_only.connection_id})
    assert status == 400 and body["error"]
    assert (await w.connections.primary(USER)).connection_id == second.connection_id
    assert first and (await call(w.app(), "put", "/api/drive/primary", {}))[0] == 400


async def test_the_folder_picker_lists_folders_by_name_under_a_parent():
    w = World()
    conn = await w.connect()
    outer = await w.folder(conn, "R&D")
    await w.folder(conn, "Coimbra 2024")
    await w.folder(conn, "Inbox", parent=outer)
    w.adapter.plant(conn.handle, (await w.adapter.get_root(conn.handle)).item_id, "birth-certificate.pdf")
    base = f"/api/drive/connections/{conn.connection_id}/folders"
    _, top = await call(w.app(), "get", base)
    assert [f["name"] for f in top["folders"]] == ["Coimbra 2024", "R&D"]
    _, inner = await call(w.app(), "get", f"{base}?parent={outer}")
    assert [(f["name"], f["path"]) for f in inner["folders"]] == [("Inbox", "R&D/Inbox")]


# ------------------------------------------------------------------------------------------------------ disconnect

async def test_disconnect_ends_the_connection_removes_the_grant_forgets_and_enqueues_the_purge():
    w = World()
    conn = await w.connect()
    status, body = await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}")
    assert (status, body) == (200, {"success": True})
    assert (await w.connections.get(USER, conn.connection_id)).state is ConnectionState.DISCONNECTED
    assert await w.store.repo.list_keys(USER, KIND) == []
    assert w.adapter.forgotten == [conn.connection_id]
    assert [t["connection_id"] for t in w.drive_tasks()] == [conn.connection_id]


async def test_disconnecting_the_primary_names_its_heir():
    w = World()
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    status, body = await call(w.app(), "delete", f"/api/drive/connections/{first.connection_id}",
                              {"heir_id": second.connection_id})
    assert status == 200
    assert (await w.connections.primary(USER)).connection_id == second.connection_id


async def test_a_bad_heir_is_400_with_the_line_and_nothing_ends():
    w = World()
    first = await w.connect()
    status, body = await call(w.app(), "delete", f"/api/drive/connections/{first.connection_id}",
                              {"heir_id": first.connection_id})
    assert (status, body["error"]) == (400, HEIR_INVALID_LINE)
    assert (await w.connections.get(USER, first.connection_id)).state is ConnectionState.ACTIVE
    assert await w.store.repo.list_keys(USER, KIND)


async def test_the_last_drive_turns_the_deep_research_copy_off_and_another_drive_leaves_it():
    w = World(copy_on=True)
    first = await w.connect()
    second = await w.connect(subject="subject-b", identity="microsoft_onedrive:drive-b")
    await call(w.app(), "delete", f"/api/drive/connections/{first.connection_id}", {"heir_id": second.connection_id})
    assert w.users.profile.config.drive_copy_deep_research is True and w.users.updates == 0
    await call(w.app(), "delete", f"/api/drive/connections/{second.connection_id}")
    assert w.users.profile.config.drive_copy_deep_research is False and w.users.updates == 1


async def test_a_failed_copy_reset_does_not_fail_the_disconnect():
    w = World(copy_on=True)
    conn = await w.connect()
    w.users.fail_update = True
    status, body = await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}")
    assert (status, body) == (200, {"success": True})


async def test_disconnect_without_the_flow_is_501_and_an_unknown_connection_404():
    w = World()
    conn = await w.connect()
    assert (await call(w.app(drive_disconnect=None), "delete", f"/api/drive/connections/{conn.connection_id}"))[0] == 501
    assert (await call(w.app(), "delete", "/api/drive/connections/conn-404"))[0] == 404


# ------------------------------------------------------------------------------------------------------ security

@pytest.mark.parametrize("method, suffix, body", [
    ("put", "/name", {"name": "Acme"}),
    ("put", "/scope", {"whole_drive": True, "folder_ids": []}),
    ("put", "/write-folder", {"folder_id": ""}),
    ("put", "/searched-by-default", {"enabled": False}),
    ("put", "/indexing", {"enabled": True}),
    ("get", "/folders", None),
    ("delete", "", None),
])
async def test_another_users_connection_answers_404_and_changes_nothing(method, suffix, body):
    w = World()
    mine = await w.connect()
    theirs = await w.connect(user=OTHER, subject="subject-c", identity="microsoft_onedrive:drive-c")
    before = await w.connections.get(OTHER, theirs.connection_id)
    status, _ = await call(w.app(), method, f"/api/drive/connections/{theirs.connection_id}{suffix}", body)
    assert status == 404
    assert await w.connections.get(OTHER, theirs.connection_id) == before
    assert await w.store.repo.list_keys(OTHER, KIND)
    assert w.drive_tasks() == [] and w.adapter.forgotten == []
    assert mine


async def test_primary_route_refuses_another_users_connection():
    w = World()
    await w.connect()
    theirs = await w.connect(user=OTHER, subject="subject-c", identity="microsoft_onedrive:drive-c")
    assert (await call(w.app(), "put", "/api/drive/primary", {"connection_id": theirs.connection_id}))[0] == 404
    assert (await w.connections.primary(OTHER)).connection_id == theirs.connection_id


async def test_a_disconnected_connection_is_404_for_its_own_user_too():
    w = World()
    conn = await w.connect()
    await w.connections.disconnect(USER, conn.connection_id)
    assert (await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/name", {"name": "X"}))[0] == 404


# ------------------------------------------------------------------------------------------------------ retired

@pytest.mark.parametrize("method, path", [("get", "/api/drive/status"), ("get", "/api/drive/indexing"),
                                           ("put", "/api/drive/indexing"), ("delete", "/api/drive/disconnect")])
async def test_the_retired_routes_are_gone(method, path):
    w = World()
    await w.connect()
    status, _ = await call(w.app(), method, path, {"enabled": True} if method == "put" else None)
    assert status in (404, 405)


# ------------------------------------------------------------------------------------------------------ carried over
# Behaviour the retired routes' tests pinned that the 2b routes keep unchanged (reviewer, slice 2b wave 2): the same
# cases, now through the connection list and the per-connection routes.

async def test_the_mirror_root_flag_is_false_without_a_root_and_a_failing_lookup_never_breaks_the_list(caplog):
    w = World()
    await w.connect()

    async def no_root(user_id):
        return False
    w.search.has_mirror_root = no_root
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200 and body["mirror_root"] is False

    async def broken(user_id):
        raise RuntimeError("boom")
    w.search.has_mirror_root = broken
    status, body = await call(w.app(), "get", "/api/drive/connections")
    assert status == 200 and body["mirror_root"] is False and len(body["connections"]) == 1


async def test_without_the_index_service_a_row_has_no_index_block():
    w = World()
    await w.connect()
    status, body = await call(w.app(drive_search_service=None), "get", "/api/drive/connections")
    assert status == 200 and body["connections"][0]["indexing"] is None and body["mirror_root"] is False


async def test_a_failing_index_read_is_logged_as_an_error(caplog):
    w = World()
    await w.connect()
    w.search.fail = True
    with caplog.at_level(logging.ERROR):
        assert (await call(w.app(), "get", "/api/drive/connections"))[0] == 200
    assert any(r.levelno == logging.ERROR and "indexing status" in r.getMessage() for r in caplog.records)


async def test_a_store_failure_of_the_list_is_500_and_leaks_nothing():
    w = World()
    await w.connect()

    async def broken(user_id):
        raise RuntimeError("token=secret")
    w.repo.list_for_user = broken
    async with w.app().test_client() as client:
        resp = await client.get("/api/drive/connections", headers=AUTH)
        text = await resp.get_data(as_text=True)
    assert resp.status_code == 500 and "secret" not in text
    assert await resp.get_json() == {"error": "Internal server error"}


async def test_after_the_last_drive_is_disconnected_the_list_asks_no_reconnect():
    w = World()
    conn = await w.connect()
    assert (await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}"))[0] == 200
    _, body = await call(w.app(), "get", "/api/drive/connections")
    assert (body["connections"], body["reconnect_needed"]) == ([], False)


async def test_a_failed_enqueue_keeps_the_indexing_switch(caplog):
    w = World()
    conn = await w.connect()

    async def refused(*args, **kwargs):
        raise RuntimeError("queue down")
    w.queue.enqueue_worker_task = refused
    status, body = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing",
                              {"enabled": True})
    assert (status, body) == (200, {"enabled": True, "queued": False})
    assert (await w.connections.get(USER, conn.connection_id)).indexing_enabled is True
    assert "sync task was not queued" in caplog.text


async def test_a_store_failure_of_the_indexing_switch_is_500_and_queues_nothing():
    w = World()
    conn = await w.connect()

    async def broken(*args, **kwargs):
        raise RuntimeError("store down")
    w.repo.update = broken
    status, _ = await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing",
                           {"enabled": True})
    assert status == 500
    assert w.drive_tasks() == []


async def test_the_indexing_switch_is_stored_before_its_task_is_enqueued():
    w = World()
    conn = await w.connect()
    seen: List[bool] = []
    enqueue = w.queue.enqueue_worker_task

    async def recording(*args, **kwargs):
        seen.append((await w.connections.get(USER, conn.connection_id)).indexing_enabled)
        return await enqueue(*args, **kwargs)
    w.queue.enqueue_worker_task = recording
    assert (await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing",
                       {"enabled": True}))[0] == 200
    assert seen == [True] and len(w.drive_tasks()) == 1


async def test_the_no_queue_warning_does_not_promise_a_scheduler(caplog):
    w = World(queue=False)
    conn = await w.connect()
    with caplog.at_level(logging.WARNING):
        await call(w.app(), "put", f"/api/drive/connections/{conn.connection_id}/indexing", {"enabled": True})
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no drive queue" in m for m in warnings)
    assert not any("scheduler will pick it up" in m for m in warnings)
    assert any("no sync task will run" in m for m in warnings)


async def test_without_the_connection_service_the_indexing_switch_says_501():
    w = World()
    conn = await w.connect()
    assert (await call(w.app(drive_connections=None), "put", f"/api/drive/connections/{conn.connection_id}/indexing",
                       {"enabled": True}))[0] == 501


async def test_a_flagged_drive_can_be_disconnected():
    w = World()
    conn = await w.connect()
    await w.connections.flag_reconnect(conn.handle)
    assert (await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}"))[0] == 200
    assert (await w.connections.get(USER, conn.connection_id)).state is ConnectionState.DISCONNECTED
    assert await w.store.repo.list_keys(USER, KIND) == []


async def test_a_failed_grant_removal_is_500_after_the_connection_ended():
    w = World()
    conn = await w.connect()

    async def broken(user_id, credential_id):
        raise RuntimeError("store down")
    w.revocation.remove = broken
    status, _ = await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}")
    assert status == 500
    assert (await w.connections.get(USER, conn.connection_id)).state is ConnectionState.DISCONNECTED


async def test_a_failed_forget_does_not_fail_the_disconnect(caplog):
    w = World()
    conn = await w.connect()

    async def broken(handle):
        raise RuntimeError("cache trouble")
    w.adapter.forget = broken
    status, _ = await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}")
    assert status == 200
    assert len(w.drive_tasks()) == 1
    assert "caches failed" in caplog.text


async def test_the_last_drive_with_the_copy_off_writes_no_profile():
    w = World(copy_on=False)
    conn = await w.connect()
    assert (await call(w.app(), "delete", f"/api/drive/connections/{conn.connection_id}"))[0] == 200
    assert w.users.updates == 0 and w.users.profile.config.drive_copy_deep_research is False
