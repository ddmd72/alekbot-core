"""The owner-notes fields of the sync state (DRIVE_FILE_NOTES_RFC §3.4) through the Firestore codec: `note_checks` (a
list of maps whose `last_seen_at` is a timestamp) and `notes_audited_at` read back as written, both are saved by
`save_sync_state` (STATE_FIELDS), and an old document without them reads the neutral defaults."""
from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import STATE_FIELDS, state_from_doc, state_to_doc
from src.domain.drive_index import DriveSyncState
from src.domain.drive_note_check import WHY_GONE, WHY_UNSTORED, NoteCheck

T = datetime(2026, 10, 22, 9, 15, 30, 250000, tzinfo=timezone.utc)


def _stored(value):
    if isinstance(value, datetime):
        return DatetimeWithNanoseconds(value.year, value.month, value.day, value.hour, value.minute, value.second,
                                       value.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: _stored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stored(v) for v in value]
    return value


def test_round_trip():
    checks = (NoteCheck("n1", "i1", WHY_GONE, T), NoteCheck("n2", "i2", WHY_UNSTORED, None))
    state = DriveSyncState(user_id="u", connection_id="u", note_checks=checks, notes_audited_at=T)
    doc = _stored(state_to_doc(state))
    assert doc["note_checks"][0] == {"note_id": "n1", "item_id": "i1", "why": "gone", "last_seen_at": T}
    back = state_from_doc("u", doc)
    assert back.note_checks == checks and back.notes_audited_at == T
    assert type(back.note_checks[0].last_seen_at) is datetime and type(back.notes_audited_at) is datetime


def test_saved_with_the_state():
    assert {"note_checks", "notes_audited_at"} <= set(STATE_FIELDS)


def test_an_old_document_reads_the_defaults():
    back = state_from_doc("u", {"drive_id": "d1"})
    assert back.note_checks == () and back.notes_audited_at is None


def test_a_malformed_entry_is_skipped():
    back = state_from_doc("u", {"note_checks": [{"note_id": "", "item_id": "i"}, {"note_id": "n", "item_id": "i",
                                                                                 "why": "gone"}]})
    assert back.note_checks == (NoteCheck("n", "i", "gone", None),)
