"""Transcript fields of the record and the sync state round-trip through the Firestore codecs (§27.14 T1)."""
from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc, state_from_doc, state_to_doc
from src.domain.drive_index import DriveIndexRecord, DriveSyncState

T = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)


def _stored(doc):
    out = {}
    for k, v in doc.items():
        out[k] = (DatetimeWithNanoseconds(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond,
                                          tzinfo=timezone.utc) if isinstance(v, datetime) else v)
    return out


def _rec(**kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", **kw)


def test_transcript_fields_roundtrip():
    rec = _rec(transcript_state="ready", transcript_key="k:1", transcript_version=1, transcript_wake_at=T,
               transcript_attempts=2, transcript_hold="budget", transcript_hold_version="1:abc",
               transcript_stub="too_long", transcript_seconds=3600, transcript_speakers=3,
               transcript_languages=("uk", "en"), transcript_body_version=2)
    doc = record_to_doc(rec)
    assert doc["transcript_languages"] == ["uk", "en"]
    back = record_from_doc(_stored(doc))
    assert back == rec
    assert type(back.transcript_wake_at) is datetime and isinstance(back.transcript_languages, tuple)


def test_purge_keep_transcripts_roundtrip():
    state = DriveSyncState(user_id="u", connection_id="u", purge_keep_transcripts=True)
    back = state_from_doc("u", _stored(state_to_doc(state)))
    assert back.purge_keep_transcripts is True and back == state


def test_old_doc_without_fields_reads_defaults():
    rec = record_from_doc({"user_id": "u", "item_id": "i"})
    assert (rec.transcript_state, rec.transcript_key, rec.transcript_version, rec.transcript_wake_at,
            rec.transcript_attempts, rec.transcript_hold, rec.transcript_hold_version, rec.transcript_stub,
            rec.transcript_seconds, rec.transcript_speakers, rec.transcript_languages,
            rec.transcript_body_version) == ("none", "", 0, None, 0, None, "", "", 0, 0, (), 0)
    assert state_from_doc("u", {}).purge_keep_transcripts is False
