"""The accuracy fields and graded values through the Firestore codec (USER_DRIVE_INDEXING_RFC §26.4, Δ47, Δ59, Δ61):
the four plain record fields and the profile's `graded` round-trip, a document stored before them loads with defaults,
and a profile stored without `graded` loads with ()."""
from datetime import date

from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_enrichment import DriveProfile, Grade, GradedField, GradedValue, ProfileDate
from src.domain.drive_index import DriveIndexRecord

PROFILE = DriveProfile(
    title="t", what_it_is="w", language="es", doc_type="id_document",
    dates=(ProfileDate("valid_until", date(2031, 5, 7), "07 05 2031"),),
    graded=(GradedValue("date", "2031-05-07", Grade.IN_TEXT_AS.value, "07 05 2031"),
            GradedValue(GradedField.ENTITY.value, "Oslo", Grade.NOT_IN_TEXT.value)))


def test_the_four_fields_are_record_fields():
    assert {"card_accuracy", "accuracy_version", "pages_refused", "read_by_fallback"} <= set(RECORD_FIELDS)


def test_they_round_trip_as_plain_values():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", card_accuracy=88, accuracy_version=1, pages_refused=2,
                           read_by_fallback=True, profile=PROFILE, profile_source="model")
    doc = record_to_doc(rec)
    assert doc["card_accuracy"] == 88 and doc["accuracy_version"] == 1 and doc["pages_refused"] == 2
    assert doc["read_by_fallback"] is True
    assert doc["profile"]["graded"] == [
        {"field": "date", "value": "2031-05-07", "grade": "in_text_as", "seen_as": "07 05 2031"},
        {"field": "entity", "value": "Oslo", "grade": "not_in_text", "seen_as": ""}]
    assert record_from_doc(doc) == rec


def test_a_score_of_zero_survives_the_round_trip():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", card_accuracy=0)
    assert record_from_doc(record_to_doc(rec)).card_accuracy == 0


def test_a_legacy_document_loads_with_defaults():
    doc = {k: v for k, v in record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="i", profile=PROFILE)).items()
           if k not in ("card_accuracy", "accuracy_version", "pages_refused", "read_by_fallback")}
    back = record_from_doc(doc)
    assert (back.card_accuracy, back.accuracy_version, back.pages_refused, back.read_by_fallback) == (None, 0, 0, False)


def test_a_legacy_profile_loads_with_no_grades():
    doc = record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="i", profile=PROFILE))
    del doc["profile"]["graded"]
    assert record_from_doc(doc).profile.graded == ()
