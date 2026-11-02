"""DriveDisconnectService (MULTI_DRIVE_RFC §4.3, §4.4, §11.10, §14 row 2b; slice 2b P-F): the second connection
disconnects and the first stays intact (credential, notes, index); the notes of the gone drive are archived and found
by the deleted-files search; the primary passes to the heir; the grant is revoked only for the last user of it; the
adapter's caches are dropped; a toggle task is queued; a re-run repeats safely. Over the in-memory stores."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from itertools import count
from unittest.mock import AsyncMock

import pytest

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index_params import TICK_DEADLINE_S
from src.domain.drive_note import (
    DriveNote, NoteBinding, NoteSnapshot, NoteState, connection_binding_key, item_binding_key, tagged_ref_key,
)
from src.domain.drive_task_keys import DRIVE_SYNC_TASK
from src.domain.drive_setting_refused import DriveSettingRefused
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount
from src.ports.user_drive_port import UserDrivePort
from src.services.credential_revocation_service import CredentialRevocationService
from src.services.drive_connection_service import DriveConnectionService
from src.services.drive_disconnect_service import DriveDisconnectService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.unit.services.drive_index_fakes import FakeQueue

KIND = "microsoft_onedrive"
U = "usr-1"
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
SEEN = T0 - timedelta(days=2)


def _item_note(tag: str, name: str, note_id: str) -> DriveNote:
    return DriveNote(note_id=note_id, user_id=U, state=NoteState.ACTIVE, current_version=1, text="Tom's plan",
                     binding=NoteBinding(item_id=f"x-{tag}", identity=f"fake:{tag}:x", ref_tag=tag),
                     snapshot=NoteSnapshot(name=name, path=f"R&D/{name}", snapshot_at=SEEN),
                     match_keys=[item_binding_key(f"fake:{tag}:x"), tagged_ref_key(tag, f"x-{tag}")])


def _drive_note(tag: str, connection_id: str, note_id: str) -> DriveNote:
    return DriveNote(note_id=note_id, user_id=U, state=NoteState.ACTIVE, current_version=1,
                     text="Acme work files: R&D, Tom's plan",
                     binding=NoteBinding(item_id="", ref_tag=tag, connection_id=connection_id),
                     snapshot=NoteSnapshot(name="Acme", is_folder=True),
                     match_keys=[connection_binding_key(connection_id)])


class World:
    def __init__(self, notes: bool = True) -> None:
        self.repo = FakeDriveConnectionRepository()
        self.store = CredentialsStore()
        self.ticks = count()
        self.now = T0
        self.connections = DriveConnectionService(
            self.repo, self.store.repo, clock=lambda: T0 + timedelta(minutes=next(self.ticks)),
            new_id=(f"conn-{n}" for n in count(1)).__next__, new_tag=iter(["a1", "b2", "c3"]).__next__)
        self.revocation = CredentialRevocationService(self.store.repo, {})
        self.drive = AsyncMock(spec=UserDrivePort)
        self.notes = InMemoryDriveNoteRepository()
        self.queue = FakeQueue()
        self.service = DriveDisconnectService(self.connections, self.revocation, self.drive,
                                              self.notes if notes else None, self.queue, clock=lambda: self.now)

    async def connect(self, subject: str, identity: str):
        credential = creds(U, KIND, account=ProviderAccount(realm="microsoft", subject=subject, label="x"))
        credential_id = await self.store.repo.save(credential)
        return await self.connections.activate(replace(credential, credential_id=credential_id), identity)

    async def two(self):
        first = await self.connect("alex@example.com", f"{KIND}:drive-a")
        second = await self.connect("tom@acme.example", f"{KIND}:drive-b")
        self.give_write_folder(first.connection_id)   # the primary is the connection that has a write folder
        await self.repo.save_settings(DriveUserSettings(user_id=U, primary_connection_id=first.connection_id))
        return first, second

    async def credential_ids(self):
        return {k.credential_id for k in await self.store.repo.list_keys(U, KIND)}

    def give_write_folder(self, connection_id: str, folder: str = "f-rd") -> None:
        key = (U, connection_id)
        self.repo.connections[key] = replace(self.repo.connections[key], write_folder_id=folder)

    def put(self, note: DriveNote) -> None:
        self.notes.notes[(note.user_id, note.note_id)] = note


async def test_the_second_connection_goes_and_the_first_is_untouched():
    w = World()
    first, second = await w.two()
    w.put(_item_note("a1", "Tom's plan.docx", "n-first"))
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    w.put(_drive_note("b2", second.connection_id, "n-drive-second"))

    assert await w.service.disconnect(U, second.connection_id) is True

    assert (await w.connections.get(U, second.connection_id)).state is ConnectionState.DISCONNECTED
    assert (await w.connections.get(U, first.connection_id)).state is ConnectionState.ACTIVE
    assert await w.credential_ids() == {first.credential_id}
    states = {n.note_id: n.state for n in w.notes.notes.values()}
    assert states == {"n-first": NoteState.ACTIVE.value, "n-second": NoteState.ARCHIVED.value,
                      "n-drive-second": NoteState.ARCHIVED.value}
    assert w.repo.settings[U].primary_connection_id == first.connection_id


async def test_an_archived_note_keeps_the_time_it_was_last_seen():
    w = World()
    _, second = await w.two()
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    await w.service.disconnect(U, second.connection_id)
    note = w.notes.notes[(U, "n-second")]
    assert (note.archived_at, note.last_seen_at) == (T0, SEEN)


async def test_the_archived_notes_are_found_by_the_deleted_files_listing():
    w = World()
    _, second = await w.two()
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    await w.service.disconnect(U, second.connection_id)
    assert [n.note_id for n in await w.notes.archived(U, 10)] == ["n-second"]
    assert await w.notes.active(U, 10) == []


async def test_the_primary_passes_to_the_heir():
    w = World()
    first, second = await w.two()
    w.give_write_folder(second.connection_id)
    await w.service.disconnect(U, first.connection_id, heir_id=second.connection_id)
    assert w.repo.settings[U].primary_connection_id == second.connection_id


async def test_without_an_heir_the_primary_is_cleared():
    w = World()
    first, _ = await w.two()
    await w.service.disconnect(U, first.connection_id)
    assert w.repo.settings[U].primary_connection_id is None


async def test_a_refused_heir_changes_nothing():
    w = World()
    first, second = await w.two()
    w.put(_item_note("a1", "Tom's plan.docx", "n-first"))
    with pytest.raises(DriveSettingRefused):
        await w.service.disconnect(U, first.connection_id, heir_id=second.connection_id)   # no write folder
    assert (await w.connections.get(U, first.connection_id)).state is ConnectionState.ACTIVE
    assert w.notes.notes[(U, "n-first")].state == NoteState.ACTIVE.value
    assert await w.credential_ids() == {first.credential_id, second.credential_id}
    assert w.queue.tasks == []


async def test_the_grant_stays_while_another_live_connection_uses_it():
    w = World()
    first = await w.connect("alex@example.com", f"{KIND}:drive-a")
    w.repo.connections[(U, "conn-x")] = replace(first, connection_id="conn-x", ref_tag="z9",
                                                drive_identity=f"{KIND}:drive-z")
    await w.service.disconnect(U, first.connection_id)
    assert await w.credential_ids() == {first.credential_id}


async def test_forget_and_the_toggle_task():
    w = World()
    _, second = await w.two()
    await w.service.disconnect(U, second.connection_id)
    w.drive.forget.assert_awaited_once_with(second.handle)
    [task] = w.queue.tasks
    assert task["task_type"] == DRIVE_SYNC_TASK
    assert task["payload"] == {"user_id": U, "connection_id": second.connection_id}
    assert task["deadline"] == TICK_DEADLINE_S
    assert ":toggle:" in task["key"]


async def test_a_rerun_on_a_disconnected_connection_repeats_safely():
    w = World()
    first, second = await w.two()
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    assert await w.service.disconnect(U, second.connection_id) is True
    w.drive.forget.reset_mock()
    w.now = T0 + timedelta(minutes=5)
    assert await w.service.disconnect(U, second.connection_id) is False
    assert w.drive.forget.await_count == 1
    assert len(w.queue.tasks) == 2
    assert (await w.connections.get(U, first.connection_id)).state is ConnectionState.ACTIVE
    assert await w.credential_ids() == {first.credential_id}


async def test_an_unknown_connection_is_a_no_op():
    w = World()
    assert await w.service.disconnect(U, "conn-none") is False
    assert w.queue.tasks == []


async def test_a_failing_note_archive_does_not_stop_the_flow():
    w = World()
    _, second = await w.two()
    w.put(_item_note("b2", "Tom's plan.docx", "n-second"))
    w.notes.archive = AsyncMock(side_effect=RuntimeError("store down"))
    await w.service.disconnect(U, second.connection_id)
    assert len(w.queue.tasks) == 1
    assert await w.credential_ids() != set()


async def test_without_a_queue_or_notes_the_flow_still_ends_the_connection():
    w = World(notes=False)
    w.service = DriveDisconnectService(w.connections, w.revocation, w.drive, None, None)
    _, second = await w.two()
    assert await w.service.disconnect(U, second.connection_id) is True
