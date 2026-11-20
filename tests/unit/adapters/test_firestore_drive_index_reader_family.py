"""U5 frozen core: the record's `reader_family` is written under its own name and read back. A document written before
the field existed reads as "" (unknown), with no migration: the every-field case
(test_firestore_drive_index_repo_serialization.py)."""
from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord


def test_the_family_round_trips():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", extracted_by="parser@4", reader_family="eml")
    doc = record_to_doc(rec)
    assert "reader_family" in RECORD_FIELDS and doc["reader_family"] == "eml"
    assert record_from_doc(doc) == rec
