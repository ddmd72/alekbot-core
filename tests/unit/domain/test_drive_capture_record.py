"""Capture on the record (USER_DRIVE_INDEXING_RFC §26.3, D34, Δ47, Δ57): `file_capture` per kind, what the planner's
`triaged`, `indexed` and `demoted` write, and the Firestore codec keeping both fields as the plain strings the
rollback tag ignores."""
from datetime import datetime, timezone

from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_capture import CaptureSignals, file_capture
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import EXTRACTION_VERSION, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict
from src.domain.drive_index_planner import demoted, indexed, triaged
from src.domain.drive_media_facts import DriveMediaFacts

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
SCANNER = CaptureSignals(pages=1, raster_pages=1, median_dpi=200.0, page_short_mm=140.0, page_long_mm=190.0)
CAMERA = DriveMediaFacts(camera_make="Apple")


def rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="a.jpg", kind="image", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1")
    base.update(kw)
    return DriveIndexRecord(**base)


class TestFileCapture:
    def test_a_pdf_takes_its_signals_then_the_model(self):
        assert file_capture("pdf", signals=SCANNER, model="screenshot") == ("scan", "detected")
        assert file_capture("pdf", signals=None, model="screenshot") == ("screenshot", "automatic")
        assert file_capture("pdf", signals=None, model="") == ("", "")

    def test_a_pdf_ignores_a_triage_word_and_a_camera_facet(self):
        assert file_capture("pdf", media=CAMERA, triaged="scan") == ("", "")

    def test_an_image_takes_the_camera_then_the_reader_then_triage(self):
        assert file_capture("image", media=CAMERA, model="scan", triaged="screenshot") == ("document_photo", "detected")
        assert file_capture("image", model="scan", triaged="screenshot") == ("scan", "automatic")
        assert file_capture("image", triaged="screenshot") == ("screenshot", "automatic")
        assert file_capture("image") == ("", "")

    def test_a_signals_free_camera_less_image_with_a_taken_date_only_has_no_camera(self):
        assert file_capture("image", media=DriveMediaFacts(taken_at=datetime(2026, 1, 1, tzinfo=timezone.utc))) == ("", "")

    def test_other_text_kinds_are_digital_detected_whatever_else_is_said(self):
        for kind in ("docx", "xlsx", "pptx", "txt", "md", "html", "eml", "epub"):
            assert file_capture(kind, signals=SCANNER, media=CAMERA, model="scan", triaged="scan") == \
                ("digital", "detected")


class TestPlanner:
    def test_a_text_verdict_stores_triages_capture_as_automatic(self):
        out = triaged(rec(), TriageResult("x", TriageVerdict.TEXT, capture="screenshot"), NOW)
        assert (out.capture, out.capture_source) == ("screenshot", "automatic")

    def test_a_camera_facet_beats_triages_word(self):
        out = triaged(rec(media=CAMERA), TriageResult("x", TriageVerdict.TEXT, capture="screenshot"), NOW)
        assert (out.capture, out.capture_source) == ("document_photo", "detected")

    def test_a_photograph_clears_any_capture(self):
        out = triaged(rec(capture="scan", capture_source="detected", media=CAMERA),
                      TriageResult("x", TriageVerdict.PHOTO), NOW)
        assert (out.capture, out.capture_source) == ("", "")

    def test_a_retriage_without_a_word_keeps_the_readers_on_an_indexed_image(self):
        done = rec(state=RecordState.INDEXED, indexed_generation=1, indexed_version="c1", extraction_version=EXTRACTION_VERSION,
                   capture="scan", capture_source="automatic")
        out = triaged(done, TriageResult("x", TriageVerdict.TEXT), NOW)
        assert (out.capture, out.capture_source) == ("scan", "automatic")

    def test_indexed_writes_the_outcomes_capture(self):
        outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="hello world", capture="scan",
                                    capture_source="detected")
        out = indexed(rec(kind="pdf", index_generation=1), outcome=outcome, generation=1, chunk_count=1, now=NOW)
        assert (out.capture, out.capture_source) == ("scan", "detected")

    def test_a_content_demotion_clears_it(self):
        out = demoted(rec(kind="pdf", capture="scan", capture_source="detected"), Reason.NO_TEXT, NOW)
        assert (out.capture, out.capture_source) == ("", "")


class TestCodec:
    def test_both_fields_are_stored_as_plain_strings(self):
        doc = record_to_doc(rec(capture="scan", capture_source="detected"))
        assert doc["capture"] == "scan" and doc["capture_source"] == "detected"
        assert {"capture", "capture_source"} <= set(RECORD_FIELDS)

    def test_round_trip(self):
        original = rec(capture="document_photo", capture_source="automatic")
        assert record_from_doc(record_to_doc(original)) == original

    def test_a_record_stored_before_capture_reads_as_none(self):
        doc = {k: v for k, v in record_to_doc(rec()).items() if k not in ("capture", "capture_source")}
        out = record_from_doc(doc)
        assert (out.capture, out.capture_source) == ("", "")
