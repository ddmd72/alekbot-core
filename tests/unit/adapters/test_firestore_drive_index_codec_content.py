"""FirestoreDriveIndexRepository codec for the Slice 2+4 fields (USER_DRIVE_INDEXING_RFC §3.2, §3.5, §24.4, §24.7).

The chunk methods are implemented (§24.6) and tested in test_firestore_drive_index_chunks.py."""
from datetime import datetime, timedelta, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import (
    record_from_doc, record_to_doc, state_from_doc, state_to_doc,
)
from src.domain.drive_index import (
    DocumentBrief, DriveIndexRecord, DriveSyncState, Reason, RecordState, TriageVerdict,
)

T = datetime(2026, 10, 12, 9, 15, 30, 250000, tzinfo=timezone.utc)
BRIEF = DocumentBrief(doc_type="invoice", description="Рахунок № 5",
                      doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc), parties=("ТОВ Приклад", "Acme S.L."),
                      amount="1 250.00 EUR")


def stored(value):
    """What Firestore hands back: timestamps as DatetimeWithNanoseconds, everything else as written."""
    if isinstance(value, datetime):
        return DatetimeWithNanoseconds(value.year, value.month, value.day, value.hour, value.minute, value.second,
                                       value.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: stored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [stored(v) for v in value]
    return value


def content_record(**kw):
    fields = dict(
        user_id="u", item_id="i", kind="pdf", state=RecordState.INDEXED, indexed_generation=2, indexed_version="c1",
        extraction_version=1, extracted_by="reader@1", text_chars=900, pages_total=3, chunk_count=4,
        lead_text="Договір оренди", brief=BRIEF, triage_verdict=TriageVerdict.TEXT, triage_version=1,
        triaged_content_version="c1", caption="a receipt on a table")
    return DriveIndexRecord(**{**fields, **kw})


class TestRecordCodec:
    def test_brief_is_a_plain_map(self):
        doc = record_to_doc(content_record())
        assert doc["brief"] == {"doc_type": "invoice", "description": "Рахунок № 5",
                                "doc_date": datetime(2024, 3, 15, tzinfo=timezone.utc),
                                "parties": ["ТОВ Приклад", "Acme S.L."], "amount": "1 250.00 EUR",
                                "date_grade": ""}   # §26.4 (Δ60): the date's grade, "" while ungraded
        assert doc["triage_verdict"] == "text" and doc["lead_text"] == "Договір оренди"
        assert (doc["triage_version"], doc["triaged_content_version"], doc["caption"]) == (1, "c1",
                                                                                          "a receipt on a table")

    def test_round_trip(self):
        rec = content_record()
        back = record_from_doc(stored(record_to_doc(rec)))
        assert back == rec
        assert type(back.brief.doc_date) is datetime and back.brief.doc_date.utcoffset() == timedelta(0)
        assert isinstance(back.brief.parties, tuple) and back.triage_verdict is TriageVerdict.TEXT

    def test_photo_round_trip(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="p", kind="image", state=RecordState.METADATA_ONLY,
                               reason=Reason.PHOTOGRAPH, triage_verdict=TriageVerdict.PHOTO, triage_version=1,
                               triaged_content_version="c9", caption="a cathedral at night")
        assert record_from_doc(stored(record_to_doc(rec))) == rec

    def test_brief_without_a_date(self):
        rec = content_record(brief=DocumentBrief(doc_type="receipt"))
        assert record_from_doc(stored(record_to_doc(rec))).brief == DocumentBrief(doc_type="receipt")

    def test_empty_values_read_as_defaults(self):
        rec = record_from_doc({"user_id": "u", "item_id": "i", "brief": None, "triage_verdict": None})
        assert (rec.brief, rec.triage_verdict) == (None, None)
        assert record_from_doc({"user_id": "u", "item_id": "i", "brief": {}}).brief is None

    def test_slice1_document_reads_with_content_defaults(self):
        rec = record_from_doc({"user_id": "u", "item_id": "i", "name": "a.pdf"})
        assert (rec.lead_text, rec.brief, rec.triage_verdict, rec.triage_version, rec.triaged_content_version,
                rec.caption) == ("", None, None, 0, "", "")


class TestStateCodec:
    def test_round_trip(self):
        state = DriveSyncState(user_id="u", connection_id="u", model_backoff_until=T, model_failures=3, usage_month="2026-10",
                               model_pages_used=120, triage_images_used=48)
        doc = state_to_doc(state)
        assert (doc["usage_month"], doc["model_pages_used"], doc["triage_images_used"], doc["model_failures"]) == \
            ("2026-10", 120, 48, 3)
        back = state_from_doc("u", stored(doc))
        assert back == state and type(back.model_backoff_until) is datetime

    def test_slice1_state_reads_with_defaults(self):
        state = state_from_doc("u", {"user_id": "u", "epoch": 1})
        assert (state.model_backoff_until, state.model_failures, state.usage_month, state.model_pages_used,
                state.triage_images_used) == (None, 0, "", 0, 0)


def test_the_sdk_encodes_the_content_fields():
    """The real client's value encoder accepts the brief map (a timestamp and an array inside) and the new fields."""
    from google.cloud.firestore_v1 import _helpers
    encoded = _helpers.encode_dict(record_to_doc(content_record()))
    brief = encoded["brief"].map_value.fields
    assert brief["parties"].array_value.values[0].string_value == "ТОВ Приклад"
    assert brief["doc_date"].timestamp_value == BRIEF.doc_date
    assert encoded["triage_verdict"].string_value == "text"
    assert _helpers.encode_dict(state_to_doc(DriveSyncState(user_id="u", connection_id="u", model_backoff_until=T)))
