"""Review fixes of the capture slice (USER_DRIVE_INDEXING_RFC §26.3): a demotion that read the text keeps its capture,
a re-triage keeps the reader's word over triage's, and the query side emits `cp:`/`cpa:` only for words a capture name
is stored under."""
from datetime import datetime, timezone

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageResult
from src.domain.drive_index import EXTRACTION_VERSION, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict
from src.domain.drive_index_planner import demoted, triaged
from src.domain.drive_keys import query_concepts

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="a.pdf", kind="pdf", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1")
    base.update(kw)
    return DriveIndexRecord(**base)


class TestDemotion:
    def test_a_demotion_that_read_the_text_keeps_the_capture(self):
        outcome = ExtractionOutcome(status=ExtractionStatus.TEXT, text="x " * 50, capture="scan",
                                    capture_source="detected")
        out = demoted(rec(), Reason.USER_CHUNK_CAP, NOW, outcome=outcome, chunk_count=3)
        assert (out.capture, out.capture_source) == ("scan", "detected")

    def test_a_demotion_with_no_outcome_clears_it(self):
        out = demoted(rec(capture="scan", capture_source="detected"), Reason.ENCRYPTED, NOW)
        assert (out.capture, out.capture_source) == ("", "")


class TestRetriage:
    def test_the_readers_stored_word_outranks_triages_on_an_indexed_image(self):
        done = rec(kind="image", name="a.jpg", state=RecordState.INDEXED, indexed_generation=1, indexed_version="c1",
                   extraction_version=EXTRACTION_VERSION, capture="scan", capture_source="automatic")
        out = triaged(done, TriageResult("x", TriageVerdict.TEXT, capture="screenshot"), NOW)
        assert (out.capture, out.capture_source) == ("scan", "automatic")

    def test_triages_word_fills_in_when_the_reader_said_none(self):
        done = rec(kind="image", name="a.jpg", state=RecordState.INDEXED, indexed_generation=1, indexed_version="c1",
                   extraction_version=EXTRACTION_VERSION)
        out = triaged(done, TriageResult("x", TriageVerdict.TEXT, capture="screenshot"), NOW)
        assert (out.capture, out.capture_source) == ("screenshot", "automatic")


class TestQuerySide:
    def _capture_keys(self, text):
        return {k for c in query_concepts(text).concepts for k in c.about_keys if k.startswith(("cp:", "cpa:"))}

    def test_an_ordinary_query_emits_no_capture_keys(self):
        assert self._capture_keys("договор аренды") == set()
        assert self._capture_keys("lease agreement 2024") == set()

    def test_a_capture_word_emits_both_families(self):
        keys = self._capture_keys("скани")
        assert any(k.startswith("cp:") for k in keys) and any(k.startswith("cpa:") for k in keys)

    def test_a_multi_word_name_run_emits_both(self):
        keys = self._capture_keys("captura de pantalla")
        assert any(k.startswith("cp:") for k in keys) and any(k.startswith("cpa:") for k in keys)
