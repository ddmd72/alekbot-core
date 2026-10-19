"""A photograph's scene labels on the record (owner decision D40, Δ47): plain strings in the Firestore document, read
back as a tuple, and a document stored before the field reads as empty."""
from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord, Scope, TriageVerdict


def _rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="a.jpg", kind="image", scope=Scope.NORMAL,
                triage_verdict=TriageVerdict.PHOTO)
    base.update(kw)
    return DriveIndexRecord(**base)


def test_the_field_is_a_stored_record_field():
    assert "photo_scene_labels" in RECORD_FIELDS


def test_it_round_trips_as_plain_strings():
    doc = record_to_doc(_rec(photo_scene_labels=("sunset", "beach")))
    assert doc["photo_scene_labels"] == ["sunset", "beach"]
    assert record_from_doc(doc).photo_scene_labels == ("sunset", "beach")


def test_a_legacy_document_without_it_reads_empty():
    doc = record_to_doc(_rec())
    del doc["photo_scene_labels"]
    assert record_from_doc(doc).photo_scene_labels == ()
