"""The deleted-files search for notes in 2b (MULTI_DRIVE_RFC §11.10, E-9; P-D): an archived whole-drive note is found
and labelled with the drive's last name; dormant means "bound on a connection that is not live", so a note on another
live drive is not dormant; a drive note is never dormant."""
from unittest.mock import AsyncMock

from src.adapters.in_memory_drive_note_repo import InMemoryDriveNoteRepository
from src.domain.drive_note import (
    DriveNote, NoteBinding, NoteSnapshot, NoteState, connection_binding_key, item_binding_key, tagged_ref_key,
)
from src.ports.embedding_service import EmbeddingService
from src.services.drive_note_deleted_search import DriveNoteDeletedSearch

U = "usr-1"


def _drive_note(state=NoteState.ARCHIVED) -> DriveNote:
    return DriveNote(note_id="nd", user_id=U, state=state, current_version=1, text="Acme work files: R&D, Tom's plan",
                     binding=NoteBinding(item_id="", ref_tag="a1", connection_id="conn-1"),
                     snapshot=NoteSnapshot(name="Acme", is_folder=True),
                     match_keys=[connection_binding_key("conn-1")])


def _item_note(tag: str) -> DriveNote:
    return DriveNote(note_id=f"n-{tag}", user_id=U, state=NoteState.ACTIVE, current_version=1, text="plan",
                     binding=NoteBinding(item_id="x", identity=f"fake:{tag}:x", ref_tag=tag),
                     snapshot=NoteSnapshot(name="plan.docx", path="R&D/plan.docx"),
                     match_keys=[item_binding_key(f"fake:{tag}:x"), tagged_ref_key(tag, "x")])


def _source(notes, names, live=None):
    async def mount_name_of(user_id, tag):
        return names.get(tag)

    async def live_tags(user_id):
        return set(live)
    return DriveNoteDeletedSearch(notes, AsyncMock(spec=EmbeddingService), embedding_space="s",
                                  mount_name_of=mount_name_of, nonce=lambda: "n0nce000",
                                  live_tags=live_tags if live is not None else None)


async def test_an_archived_drive_note_is_found_and_labelled_with_the_drives_last_name():
    notes = InMemoryDriveNoteRepository()
    notes.notes[(U, "nd")] = _drive_note()

    hits, _ = await _source(notes, {"a1": "Acme Hamburg"}, live=["b2"])(U, [], None)

    assert [h.name for h in hits] == ["drive Acme Hamburg"] and hits[0].path == ""
    assert hits[0].ref == "drive_note:nd" and hits[0].item_key == ""


async def test_the_label_falls_back_to_the_stored_name_when_the_drive_is_unknown():
    notes = InMemoryDriveNoteRepository()
    notes.notes[(U, "nd")] = _drive_note()

    hits, _ = await _source(notes, {}, live=[])(U, [], None)

    assert [h.name for h in hits] == ["drive Acme"]


async def test_a_note_on_another_live_drive_is_not_dormant_and_one_on_a_gone_connection_is():
    notes = InMemoryDriveNoteRepository()
    notes.notes[(U, "n-a1")] = _item_note("a1")
    notes.notes[(U, "n-b2")] = _item_note("b2")

    hits, _ = await _source(notes, {}, live=["a1"])(U, [], None)

    assert [h.ref for h in hits] == ["drive_note:n-b2"]


async def test_an_active_drive_note_is_never_dormant_even_when_its_connection_is_not_live():
    notes = InMemoryDriveNoteRepository()
    notes.notes[(U, "nd")] = _drive_note(NoteState.ACTIVE)

    hits, _ = await _source(notes, {}, live=[])(U, [], None)

    assert hits == []
