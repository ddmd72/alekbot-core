"""The sync state's `purge_keep_transcripts` reads back as a bool through the Firestore codec (§27.14 T1); the record's
transcript fields round-trip, and every default reads back, in the every-field case
(test_firestore_drive_index_repo_serialization.py)."""
from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import state_from_doc, state_to_doc
from src.domain.drive_index import DriveSyncState


def _stored(doc):
    out = {}
    for k, v in doc.items():
        out[k] = (DatetimeWithNanoseconds(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond,
                                          tzinfo=timezone.utc) if isinstance(v, datetime) else v)
    return out


def test_purge_keep_transcripts_roundtrip():
    state = DriveSyncState(user_id="u", connection_id="u", purge_keep_transcripts=True)
    back = state_from_doc("u", _stored(state_to_doc(state)))
    assert back.purge_keep_transcripts is True and back == state
