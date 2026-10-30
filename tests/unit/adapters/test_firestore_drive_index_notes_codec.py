"""The owner-note fields through the drive index codec (DRIVE_FILE_NOTES_RFC §3.3, §3.4): the record's copy and the
sync state's notes step state round-trip through the stored types; the lean projection carries `owner_note_id`; a record
or state stored before them reads back neutral defaults. Synthetic values only."""
from __future__ import annotations

from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import (
    STATE_FIELDS, lean_record_from_doc, record_from_doc, record_to_doc, state_from_doc, state_to_doc,
)
from src.domain.drive_index import DriveIndexRecord, DriveSyncState
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS

T = datetime(2026, 10, 22, 9, 30, 15, 123456, tzinfo=timezone.utc)


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


def test_the_notes_state_round_trips_with_plain_datetimes():
    state = DriveSyncState(user_id="u", connection_id="u", notes_applied_at=T, notes_applied_id="n9", notes_paging=True,
                           notes_scanned_at=T, notes_merge_version=1, notes_space="space/8", notes_space_at=T,
                           notes_space_id="n3")
    doc = state_to_doc(state)
    names = ("notes_applied_at", "notes_applied_id", "notes_paging", "notes_scanned_at", "notes_merge_version",
             "notes_space", "notes_space_at", "notes_space_id")
    assert set(names) <= set(STATE_FIELDS)
    back = state_from_doc("u", _stored(doc))
    assert back == state
    for name in ("notes_applied_at", "notes_scanned_at", "notes_space_at"):
        assert type(getattr(back, name)) is datetime


def test_old_documents_read_back_neutral_defaults():
    rec = record_from_doc({"user_id": "u", "item_id": "i"})
    assert (rec.owner_note_id, rec.owner_life_areas, rec.note_lookup_due) == ("", (), False)
    state = state_from_doc("u", {})
    assert (state.notes_applied_at, state.notes_paging, state.notes_merge_version, state.notes_space) == \
        (None, False, 0, "")


def test_the_lean_projection_carries_the_note_id():
    assert "owner_note_id" in RECORD_LEAN_FIELDS
    assert lean_record_from_doc({"item_id": "i", "owner_note_id": "n1"}).owner_note_id == "n1"
    assert lean_record_from_doc({"item_id": "i"}).owner_note_id == ""
