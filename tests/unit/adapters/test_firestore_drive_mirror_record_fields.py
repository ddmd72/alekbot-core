"""The mirror queue fields of the record (§27.9.4) round-trip through the Firestore codecs."""
from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


def test_mirror_fields_roundtrip():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", mirror_due=True, mirrored_digest="abc", mirror_retry_at=T,
                           mirror_held_digest="def")
    doc = record_to_doc(rec)
    stored = {k: (DatetimeWithNanoseconds(v.year, v.month, v.day, v.hour, v.minute, v.second, v.microsecond,
                                          tzinfo=timezone.utc) if isinstance(v, datetime) else v)
              for k, v in doc.items()}
    back = record_from_doc(stored)
    assert back == rec and type(back.mirror_retry_at) is datetime


def test_old_doc_reads_neutral_mirror_defaults():
    rec = record_from_doc({"user_id": "u", "item_id": "i"})
    assert (rec.mirror_due, rec.mirrored_digest, rec.mirror_retry_at, rec.mirror_held_digest) == (False, "", None, "")
