"""The lifecycle audit never reads a whole-drive note by id (MULTI_DRIVE_RFC §11.10, P-D): it has no item, so a read
would answer "not found" and the note would be archived."""
from datetime import datetime, timezone

from src.domain.drive_note import DriveNote, NoteBinding, NoteState, connection_binding_key, item_binding_key
from src.domain.drive_note_lifecycle import audit_candidates

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def _drive_note() -> DriveNote:
    return DriveNote(note_id="nd", user_id="usr-1", state=NoteState.ACTIVE, current_version=1, text="Acme",
                     binding=NoteBinding(item_id="", ref_tag="a1", connection_id="conn-1"),
                     match_keys=[connection_binding_key("conn-1")])


def _item_note() -> DriveNote:
    return DriveNote(note_id="ni", user_id="usr-1", state=NoteState.ACTIVE, current_version=1, text="plan",
                     binding=NoteBinding(item_id="x", identity="fake:d:x", ref_tag="a1"),
                     match_keys=[item_binding_key("fake:d:x")])


def test_a_drive_note_on_the_same_connection_tag_is_not_a_candidate_and_an_item_note_is():
    found = audit_candidates([_drive_note(), _item_note()], ref_tag="a1", now=NOW)

    assert [n.note_id for n in found] == ["ni"]
