"""A profile stored before a profile date's `printed` form still loads with "" (USER_DRIVE_INDEXING_RFC §26.4); the form's
round trip is in the every-field case (test_firestore_drive_index_repo_serialization.py)."""
from datetime import date

from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc
from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_index import DriveIndexRecord


def _record(dates):
    return DriveIndexRecord(user_id="u1", item_id="I1", profile=DriveProfile(title="Card", what_it_is="A card", language="en", doc_type="id_document",
                                                                         dates=dates))


def test_a_legacy_stored_date_without_printed_loads_with_an_empty_form():
    doc = record_to_doc(_record((ProfileDate("issued", date(2024, 3, 15)),)))
    doc["profile"]["dates"] = [{"label": "issued", "iso": "2024-03-15"}]
    assert record_from_doc(doc).profile.dates == (ProfileDate("issued", date(2024, 3, 15), ""),)
