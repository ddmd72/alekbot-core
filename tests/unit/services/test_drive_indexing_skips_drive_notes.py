"""A whole-drive note has no `i:` key, so the indexer's notes step merges nothing for it (MULTI_DRIVE_RFC §11.10, P-D)."""
from src.domain.drive_note import DriveNote, NoteBinding, NoteState, connection_binding_key
from tests.unit.services.drive_note_world import NotesWorld


async def test_the_notes_step_merges_no_record_for_a_drive_note():
    w = NotesWorld()
    await w.settle()
    w.notes.notes[("u", "nd")] = DriveNote(
        note_id="nd", user_id="u", account_id="a", state=NoteState.ACTIVE, current_version=1, text="Acme work files",
        binding=NoteBinding(item_id="", ref_tag="a1", connection_id="conn-1"),
        match_keys=[connection_binding_key("conn-1")], updated_at=w.clock.now, revision=1)
    before = dict(w.repo.records)
    w.clock.advance(5)

    await w.settle()

    assert w.repo.records == before
    assert w.note("nd").state == NoteState.ACTIVE.value
