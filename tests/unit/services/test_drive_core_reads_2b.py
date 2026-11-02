"""Two frozen reads of slice 2b (design §1.8, §1.9): `DriveNoteService.drive_note_texts` (connection id -> the text of
its active drive note; item notes, cleared notes and other users left out; the newest note of a connection wins) and
`DriveSearchService.indexing_status(connection_id=…)` (that live connection's own state and indexing switch)."""
from datetime import datetime, timedelta, timezone

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import DriveSyncState
from src.domain.drive_note import DriveNote, NoteBinding, NoteState, note_match_keys
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.provider_account import ProviderAccount
from src.services.drive_connection_service import DriveConnectionService
from src.services.drive_note_service import DriveNoteService
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

from .drive_index_fakes import FakeEmbeddings

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def note(note_id, binding, text, *, user_id="usr-1", state=NoteState.ACTIVE, minutes=0):
    return DriveNote(note_id=note_id, user_id=user_id, text=text, state=state, binding=binding,
                     match_keys=note_match_keys(binding), updated_at=T0 + timedelta(minutes=minutes))


def drive_binding(connection_id, tag):
    return NoteBinding(item_id="", connection_id=connection_id, ref_tag=tag)


async def test_drive_note_texts():
    notes = InMemoryDriveNoteRepository()
    for n in (
        note("n1", drive_binding("conn-2", "b2"), "Acme work files: R&D, Tom's plan", minutes=2),
        note("n2", drive_binding("conn-1", "a1"), "Family documents: Jamie's birth certificate", minutes=1),
        note("n3", NoteBinding(item_id="item-1", identity="fake_a:d1:item-1", ref_tag="a1"), "the lab report"),
        note("n4", drive_binding("conn-3", "c3"), "", state=NoteState.CLEARED),
        note("n5", drive_binding("conn-2", "b2"), "an older note", minutes=0),
        note("n6", drive_binding("conn-9", "z9"), "another user's", user_id="usr-2"),
    ):
        notes.notes[(n.user_id, n.note_id)] = n
    service = DriveNoteService(notes, object(), object(), object(), object(), object(),  # type: ignore[arg-type]
                               access=object(), embedding_space="space-a")
    assert await service.drive_note_texts("usr-1") == {
        "conn-2": "Acme work files: R&D, Tom's plan", "conn-1": "Family documents: Jamie's birth certificate"}


def connection(connection_id, tag, *, minutes=0, indexing=True, state=ConnectionState.ACTIVE):
    gone = state is ConnectionState.DISCONNECTED
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag,
                           created_at=T0 + timedelta(minutes=minutes), indexing_enabled=indexing, state=state,
                           disconnected_at=T0 if gone else None,
                           provider_account=ProviderAccount(realm="fake", subject=f"s-{connection_id}", label=""))


async def test_indexing_status_of_one_connection():
    conns = FakeDriveConnectionRepository()
    await conns.create(connection("conn-1", "a1"))
    await conns.create(connection("conn-2", "b2", minutes=1, indexing=False))
    await conns.create(connection("conn-3", "c3", minutes=2, state=ConnectionState.DISCONNECTED))
    await conns.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
    repo = InMemoryDriveIndexRepository()
    for cid, carded in (("conn-1", 7), ("conn-2", 3), ("conn-3", 5)):
        await repo.save_sync_state(DriveSyncState(user_id="usr-1", connection_id=cid, epoch=1, root_item_id="root",
                                                  created_at=T0, counters={"carded": carded}))
    service = DriveSearchService(repo, FakeEmbeddings(), object(), DriveConnectionService(conns, None),
                                 clock=lambda: T0, embedding_space="fake/8")
    primary = await service.indexing_status("usr-1")
    assert primary.enabled is True and primary.counters == {"carded": 7}
    second = await service.indexing_status("usr-1", connection_id="conn-2")
    assert second.enabled is False and second.counters == {"carded": 3}
    gone = await service.indexing_status("usr-1", connection_id="conn-3")
    assert gone.enabled is False and gone.counters == {}
    absent = await service.indexing_status("usr-1", connection_id="conn-9")
    assert absent.enabled is False and absent.counters == {}
