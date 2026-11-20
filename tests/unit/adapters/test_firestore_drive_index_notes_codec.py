"""The owner-note fields through the drive index codec (DRIVE_FILE_NOTES_RFC §3.3, §3.4): the record's copy is written
by the record codec and round-trips through the stored types; the lean projection carries `owner_note_id`. The notes
step state's round trip and the neutral defaults are in the every-field case
(test_firestore_drive_index_repo_serialization.py). Synthetic values only."""
from __future__ import annotations

from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import (
    lean_record_from_doc, record_from_doc, record_to_doc,
)
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS

def _stored(value):
    if isinstance(value, datetime):
        return DatetimeWithNanoseconds(value.year, value.month, value.day, value.hour, value.minute, value.second,
                                       value.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: _stored(v) for k, v in value.items()}
    return value


def test_the_record_copy_round_trips():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", owner_note_id="n1", owner_note_version=3,
                           owner_note_text="the contractor's invoice", owner_doc_type="contract",
                           owner_life_areas=("home", "finance"), owner_note_merge_version=1)
    doc = record_to_doc(rec)
    assert doc["owner_life_areas"] == ["home", "finance"] and doc["note_lookup_due"] is False
    assert record_from_doc(_stored(doc)) == rec


def test_the_lean_projection_carries_the_note_id():
    assert "owner_note_id" in RECORD_LEAN_FIELDS
    assert lean_record_from_doc({"item_id": "i", "owner_note_id": "n1"}).owner_note_id == "n1"
    assert lean_record_from_doc({"item_id": "i"}).owner_note_id == ""
