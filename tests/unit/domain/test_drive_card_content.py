"""Card text with content (USER_DRIVE_INDEXING_RFC §8.5, §24.4, §25.2.7): the brief, a photograph's description and
(machine text only) the lead text are appended only when present, so every Slice 1 card keeps its text; its hash moves
with each CARD_VERSION bump (§24.1; 3 in Search v2)."""
import hashlib
from datetime import datetime, timezone

import pytest

from src.domain.drive_card import card_hash, card_text
from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_index import (
    CARD_VERSION, ContentClass, DocumentBrief, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict,
)

PHOTO_FACETS = {"photo": {"takenDateTime": "2025-08-01T10:00:00Z", "cameraMake": "Apple", "cameraModel": "iPhone 15"},
                "image": {"width": 4032, "height": 3024}}


def rec(name="Договор.pdf", kind="pdf", **kw):
    kw.setdefault("reason", Reason.KIND_NOT_ADMITTED)
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="i", name=name, kind=kind, scope=Scope.NORMAL,
                            state=RecordState.METADATA_ONLY, **kw)


# Slice 1 shapes and the exact text Slice 1 embedded for them.
SLICE1 = [
    (rec(), "Договор.pdf | PDF document"),
    (rec("IMG_1.jpg", "image", facets=PHOTO_FACETS, place_name="Rome (Roma), Italy"),
     "IMG_1.jpg | image | taken 2025-08-01 | camera Apple iPhone 15 | place Rome (Roma), Italy | 4032x3024"),
    (rec("clip.mp4", "video", facets={"video": {"duration": 125000, "width": 1920, "height": 1080}}),
     "clip.mp4 | video | video 2:05 1920x1080"),
    (rec("notes", "other"), "notes | file"),
    (DriveIndexRecord(user_id="u", connection_id="u", item_id="f", name="Docs", is_folder=True, kind="folder",
                      content_class=ContentClass.FOLDER, scope=Scope.NORMAL, state=RecordState.METADATA_ONLY,
                      reason=Reason.FOLDER), "Docs | folder"),
    (DriveIndexRecord(user_id="u", connection_id="u", item_id="r", name="proj", is_folder=True, kind="folder",
                      content_class=ContentClass.FOLDER, scope=Scope.OPAQUE_ROOT, state=RecordState.METADATA_ONLY,
                      reason=Reason.OPAQUE_ROOT), "proj | repository"),
]


class TestSliceOneCardsUnchanged:
    @pytest.mark.parametrize("record,text", SLICE1)
    def test_text(self, record, text):
        assert card_text(record) == text

    @pytest.mark.parametrize("record,text", SLICE1)
    def test_hash(self, record, text):
        # the v2 flip re-embeds every card once (§24.1, §12.3, §25.2.7); 4: the capture segment (§26.3, §26.7)
        assert CARD_VERSION == 4
        assert card_hash(record) == hashlib.sha256(f"4\n{text}".encode("utf-8")).hexdigest()[:32]


BRIEF = DocumentBrief(doc_type="invoice", description="Рахунок за оренду офісу",
                      doc_date=datetime(2024, 3, 15, tzinfo=timezone.utc), parties=("ТОВ Приклад", "ФОП Мельник Т."),
                      amount="1 250.00 EUR")


class TestContentSegments:
    def test_brief_segments_in_order(self):
        text = card_text(rec("scan.pdf", brief=BRIEF))
        # CARD_VERSION 4 (Δ64): a stored v1 brief's doc type is never on the card (the profile's is the only one)
        assert text == ("scan.pdf | PDF document | Рахунок за оренду офісу | 2024-03-15 | "
                        "parties: ТОВ Приклад, ФОП Мельник Т. | 1 250.00 EUR")

    def test_empty_brief_fields_are_left_out(self):
        assert card_text(rec("scan.pdf", brief=DocumentBrief(doc_type="receipt"))) == \
            "scan.pdf | PDF document"   # Δ64: the doc type is not a brief segment

    def test_an_empty_brief_adds_nothing(self):
        assert card_text(rec("scan.pdf", brief=DocumentBrief())) == "scan.pdf | PDF document"

    def test_photograph_description_before_the_key_facets(self):
        # v2: the triage description (§25.4.3) takes the v1 caption's place, still before the key facets
        r = rec("IMG_2.jpg", "image", facets=PHOTO_FACETS, reason=Reason.PHOTOGRAPH,
                triage_verdict=TriageVerdict.PHOTO, photo_description="a cathedral at night")
        assert card_text(r) == ("IMG_2.jpg | image | a cathedral at night | taken 2025-08-01 | "
                                "camera Apple iPhone 15 | 4032x3024")

    def test_read_image_brief_then_facets_and_no_lead_text(self):
        # v2 (§25.2.7): a read image keeps brief-then-facets; lead text left the card (machine text only)
        r = rec("IMG_3.jpg", "image", facets=PHOTO_FACETS, brief=DocumentBrief(doc_type="receipt"),
                lead_text="TOTAL 12,50")
        assert card_text(r) == ("IMG_3.jpg | image | taken 2025-08-01 | camera Apple iPhone 15 | "
                                "4032x3024")   # Δ64: no brief doc type

    def test_lead_text_only_for_machine_text_and_last(self):
        r = rec("scan.pdf", brief=DocumentBrief(doc_type="contract"), lead_text="Договір оренди № 5")
        assert card_text(r) == "scan.pdf | PDF document"   # Δ64: no brief doc type either
        log = rec("a.log", "text", reason=Reason.MACHINE_TEXT, lead_text="2026-10-01 INFO start")
        assert card_text(log) == "a.log | text file | machine-generated text | 2026-10-01 INFO start"

    def test_each_segment_changes_the_hash(self):
        base = rec("scan.pdf")
        profile = DriveProfile(title="Lease", what_it_is="A flat lease", language="en", doc_type="contract")
        hashes = {card_hash(base), card_hash(rec("scan.pdf", brief=BRIEF)), card_hash(rec("scan.pdf", profile=profile)),
                  card_hash(rec("scan.pdf", reason=Reason.MACHINE_TEXT, lead_text="lead text")),
                  card_hash(rec("scan.pdf", "image", reason=Reason.PHOTOGRAPH, triage_verdict=TriageVerdict.PHOTO,
                                photo_description="a caption"))}
        assert len(hashes) == 5

    def test_folders_ignore_content_fields(self):
        folder, text = SLICE1[4]
        from dataclasses import replace
        assert card_text(replace(folder, lead_text="x", caption="y", brief=BRIEF)) == text
