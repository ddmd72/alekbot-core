"""U5 frozen core: the record's `reader_family` is written under its own name and read back; a document written before
the field existed reads as "" (unknown), with no migration."""
from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord


def test_the_family_round_trips():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", extracted_by="parser@4", reader_family="eml")
    doc = record_to_doc(rec)
    assert "reader_family" in RECORD_FIELDS and doc["reader_family"] == "eml"
    assert record_from_doc(doc) == rec


def test_a_document_from_before_the_field_reads_as_unknown():
    doc = record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="i", extracted_by="parser@3"))
    del doc["reader_family"]
    back = record_from_doc(doc)
    assert back.reader_family == "" and back.extracted_by == "parser@3"
