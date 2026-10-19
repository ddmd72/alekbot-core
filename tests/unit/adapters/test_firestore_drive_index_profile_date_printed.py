"""A profile date's `printed` form round-trips through the record codec; a profile stored before it still loads with
"" (USER_DRIVE_INDEXING_RFC §26.4)."""
from datetime import date

from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc
from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_index import DriveIndexRecord


def _record(dates):
    return DriveIndexRecord(user_id="u1", item_id="I1", profile=DriveProfile(title="Card", what_it_is="A card", language="en", doc_type="id_document",
                                                                         dates=dates))


def test_printed_round_trips():
    rec = _record((ProfileDate("valid_until", date(2031, 5, 7), "07 05 2031"),))
    doc = record_to_doc(rec)
    assert doc["profile"]["dates"] == [{"label": "valid_until", "iso": "2031-05-07", "printed": "07 05 2031"}]
    assert record_from_doc(doc).profile.dates == rec.profile.dates


def test_a_legacy_stored_date_without_printed_loads_with_an_empty_form():
    doc = record_to_doc(_record((ProfileDate("issued", date(2024, 3, 15)),)))
    doc["profile"]["dates"] = [{"label": "issued", "iso": "2024-03-15"}]
    assert record_from_doc(doc).profile.dates == (ProfileDate("issued", date(2024, 3, 15), ""),)
