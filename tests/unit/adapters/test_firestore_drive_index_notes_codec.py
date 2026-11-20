"""The owner-note fields through the drive index codec (DRIVE_FILE_NOTES_RFC §3.3, §3.4): the lean projection carries
`owner_note_id`. The record's copy and the notes step state round-trip, and read the neutral defaults, in the every-field
case (test_firestore_drive_index_repo_serialization.py). Synthetic values only."""
from __future__ import annotations

from src.adapters.firestore_drive_index_repo import lean_record_from_doc
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS


def test_the_lean_projection_carries_the_note_id():
    assert "owner_note_id" in RECORD_LEAN_FIELDS
    assert lean_record_from_doc({"item_id": "i", "owner_note_id": "n1"}).owner_note_id == "n1"
    assert lean_record_from_doc({"item_id": "i"}).owner_note_id == ""
