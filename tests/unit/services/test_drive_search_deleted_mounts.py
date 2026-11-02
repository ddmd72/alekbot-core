"""The deleted-files search through the mount tree (multi-drive 2a, design §1 S-12, E-12): a hit's paths are canonical
with the mount of the drive its document lives on; a hit of a drive with no connection keeps its stored path; the
owner-notes source mounts a note by the connection its ref tag names."""
import itertools
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_connection import DriveConnection
from src.domain.drive_index import DriveChange
from src.domain.drive_keys import word_keys
from src.domain.drive_mirror import (
    ARCHIVE_NAME, KIND_ARCHIVE, KIND_FILE, MIRROR_ACTIVE, MIRROR_ARCHIVED, MirrorDoc, MirrorRoot,
)
from src.domain.drive_note import DriveNote, NoteBinding, NoteSnapshot, NoteState, item_binding_key, tagged_ref_key
from src.domain.drive_user_settings import DriveUserSettings
from src.ports.drive_index_repository import DriveIndexRepository
from src.ports.user_drive_port import UserDrivePort
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _connection(connection_id: str, tag: str, name: str, identity: str, created_at: datetime) -> DriveConnection:
    return DriveConnection(connection_id=connection_id, user_id="u1", account_id="a", credential_id=f"cred-{tag}",
                           kind="fake", drive_identity=identity, ref_tag=tag, mount_name=name,
                           indexing_enabled=True, created_at=created_at)


def _doc(n: int, drive: str) -> MirrorDoc:
    return MirrorDoc(
        user_id="u1", drive_identity=drive, kind=KIND_FILE, source_item_id=f"s{n}", source_identity=f"{drive}:s{n}",
        source_name=f"call{n}.m4a", source_path=f"Calls/call{n}.m4a", content_key=f"k{n}", mirror_item_id=f"m{n}",
        mirror_parent_id=f"day-{drive}", mirror_name=f"call{n}.md", state=MIRROR_ARCHIVED, archive_day="2026-10-05",
        archived_at=T0 + timedelta(minutes=n), seconds=600.0, speakers=1, languages=("en",),
        archive_keys=tuple(sorted(word_keys("budget meeting"))), name_keys=tuple(sorted(word_keys(f"call{n}"))))


async def _service(*, with_notes: bool = False):
    mirror = InMemoryDriveMirrorRepository()
    for drive in ("fake:d1", "fake:d2", "fake:d9"):
        await mirror.save_root(MirrorRoot(user_id="u1", drive_identity=drive, root_item_id=f"root-{drive}",
                                          root_name="_Transcripts"))
        await mirror.save_doc(MirrorDoc(user_id="u1", drive_identity=drive, kind=KIND_ARCHIVE,
                                        source_item_id=f"arch-{drive}", mirror_item_id=f"arch-{drive}",
                                        mirror_parent_id=f"root-{drive}", mirror_name=ARCHIVE_NAME,
                                        state=MIRROR_ACTIVE))
        await mirror.save_doc(MirrorDoc(user_id="u1", drive_identity=drive, kind=KIND_ARCHIVE,
                                        source_item_id=f"day-{drive}", mirror_item_id=f"day-{drive}",
                                        mirror_parent_id=f"arch-{drive}", mirror_name="2026-10-05",
                                        state=MIRROR_ACTIVE))
    repo = FakeDriveConnectionRepository()
    await repo.create(_connection("conn-1", "a1", "Drive", "fake:d1", T0))
    await repo.create(_connection("conn-2", "b2", "Acme", "fake:d2", T0 + timedelta(days=1)))
    await repo.save_settings(DriveUserSettings(user_id="u1", primary_connection_id="conn-1"))
    drive = AsyncMock(spec=UserDrivePort)
    drive.get_change.side_effect = lambda handle, item_id: DriveChange(item_id=item_id)
    index = AsyncMock(spec=DriveIndexRepository)
    index.get_transcript.return_value = None
    counter = itertools.count(1)
    service = DriveSearchService(index, AsyncMock(), drive, connection_service(repo), embedding_space="s/8",
                                 nonce=lambda: f"nonce{next(counter):04d}", mirror=mirror)
    notes = InMemoryDriveNoteRepository()
    if with_notes:
        service.add_deleted_source(DriveNoteDeletedSearch(notes, AsyncMock(), embedding_space="s/8",
                                                          mount_name_of=service.mount_name_of_tag))
    return service, mirror, notes


async def test_a_hit_on_the_searched_drive_is_canonical_in_its_head_item_and_archive_line():
    service, mirror, _ = await _service()
    await mirror.save_doc(_doc(1, "fake:d1"))
    found = await service.search_deleted("u1", "budget")
    assert "call1.m4a | Drive/Calls/call1.m4a | gone from the drive" in found.text
    assert "archived transcript: drive:a1:m1 (Drive/_Transcripts/Archive/2026-10-05/call1.md)" in found.text
    assert [i.path for i in found.items] == ["Drive/_Transcripts/Archive/2026-10-05/call1.md"]


async def test_a_hit_of_another_connected_drive_carries_that_drives_mount_and_no_ref():
    service, mirror, _ = await _service()
    await mirror.save_doc(_doc(2, "fake:d2"))
    found = await service.search_deleted("u1", "budget")
    assert "call2.m4a | Acme/Calls/call2.m4a | on a drive that is not connected" in found.text
    assert "archived transcript: Acme/_Transcripts/Archive/2026-10-05/call2.md" in found.text
    assert found.items == []


async def test_a_hit_of_a_drive_with_no_connection_keeps_its_stored_path():
    service, mirror, _ = await _service()
    await mirror.save_doc(_doc(3, "fake:d9"))
    found = await service.search_deleted("u1", "budget")
    assert "call3.m4a | Calls/call3.m4a | on a drive that is not connected" in found.text
    assert "archived transcript: _Transcripts/Archive/2026-10-05/call3.md" in found.text
    assert "Drive/" not in found.text and "Acme/" not in found.text


async def test_a_note_hit_is_mounted_by_the_connection_its_tag_names():
    service, mirror, notes = await _service(with_notes=True)
    binding = NoteBinding(identity="fake:d2:i7", item_id="i7", ref_tag="b2")
    note = DriveNote(
        note_id="n7", user_id="u1", account_id="a", state=NoteState.ARCHIVED, current_version=1,
        text="contractor invoice", binding=binding, snapshot=NoteSnapshot(name="scan7.pdf", path="Docs/scan7.pdf"),
        match_keys=[item_binding_key(binding.identity), tagged_ref_key("b2", "i7")], archived_at=T0, updated_at=T0)
    notes.notes[("u1", "n7")] = note
    found = await service.search_deleted("u1", "")
    assert "scan7.pdf | Acme/Docs/scan7.pdf |" in found.text


async def test_a_note_of_a_tag_the_user_never_had_keeps_its_stored_path():
    service, _, notes = await _service(with_notes=True)
    binding = NoteBinding(identity="fake:d9:i8", item_id="i8", ref_tag="c3")
    notes.notes[("u1", "n8")] = DriveNote(
        note_id="n8", user_id="u1", account_id="a", state=NoteState.ARCHIVED, current_version=1, text="invoice",
        binding=binding, snapshot=NoteSnapshot(name="scan8.pdf", path="Docs/scan8.pdf"),
        match_keys=[item_binding_key(binding.identity), tagged_ref_key("c3", "i8")], archived_at=T0, updated_at=T0)
    found = await service.search_deleted("u1", "")
    assert "scan8.pdf | Docs/scan8.pdf |" in found.text


async def test_a_note_source_without_a_mount_lookup_prints_the_stored_path():
    service, _, notes = await _service()
    service.add_deleted_source(DriveNoteDeletedSearch(notes, AsyncMock(), embedding_space="s/8"))
    binding = NoteBinding(identity="fake:d1:i9", item_id="i9", ref_tag="a1")
    notes.notes[("u1", "n9")] = DriveNote(
        note_id="n9", user_id="u1", account_id="a", state=NoteState.ARCHIVED, current_version=1, text="invoice",
        binding=binding, snapshot=NoteSnapshot(name="scan9.pdf", path="Docs/scan9.pdf"),
        match_keys=[item_binding_key(binding.identity), tagged_ref_key("a1", "i9")], archived_at=T0, updated_at=T0)
    assert "scan9.pdf | Docs/scan9.pdf |" in (await service.search_deleted("u1", "")).text


async def test_a_tag_resolves_to_its_mount_name_and_an_unknown_or_empty_tag_to_none():
    service, _, _ = await _service()
    assert await service.mount_name_of_tag("u1", "b2") == "Acme"
    assert await service.mount_name_of_tag("u1", "zz9") is None
    assert await service.mount_name_of_tag("u1", "") is None
