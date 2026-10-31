"""Capture (USER_DRIVE_INDEXING_RFC §26.3, D34, Δ57): the deterministic rules in order, the ambiguous case going to the
model, a deterministic answer beating the model's, Graph's camera facet, and the child's report checked like any
other field it writes."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_capture import (
    CAPTURE_LABELS, Capture, CaptureSignals, CaptureSource, camera_capture, detect, model_capture, settle,
    signals_from_json, signals_to_json,
)
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_index_params import FULL_PAGE_COVER, SCAN_DPI_MAX, SCAN_DPI_MIN, SCAN_GLASS_MAX_MM

SCAN = CaptureSignals(pages=2, text_pages=0, raster_pages=2, median_dpi=200.0, page_short_mm=204.0,
                      page_long_mm=291.0)


class TestParameters:
    def test_the_rfc_values(self):
        assert FULL_PAGE_COVER == 0.95
        assert (SCAN_DPI_MIN, SCAN_DPI_MAX) == (150, 600)
        assert SCAN_GLASS_MAX_MM == (220, 360)


class TestRules:
    def test_rule_2_a_scanner_pdf_is_a_scan(self):
        assert detect(SCAN) is Capture.SCAN

    def test_rule_2_holds_with_an_ocr_text_layer(self):
        # Δ57: a searchable scan is still a machine reading of a page image
        assert detect(CaptureSignals(**{**signals_to_json(SCAN), "text_pages": 2})) is Capture.SCAN

    def test_rule_1_a_camera_in_the_exif_comes_first(self):
        assert detect(CaptureSignals(**{**signals_to_json(SCAN), "exif_camera": True})) is Capture.DOCUMENT_PHOTO

    def test_rule_3_text_on_every_page_and_no_raster_is_digital(self):
        assert detect(CaptureSignals(pages=3, text_pages=3, raster_pages=0, page_short_mm=210,
                                     page_long_mm=297)) is Capture.DIGITAL

    @pytest.mark.parametrize("change", [
        {"raster_pages": 1},                                   # one page of two is not a full-page raster
        {"median_dpi": 72.0},                                  # below a scanner's resolution
        {"median_dpi": 1200.0},                                # above it
        {"page_long_mm": 420.0},                               # A3: larger than a scanner glass
        {"page_short_mm": 230.0},
    ])
    def test_not_every_scan_condition_is_ambiguous(self, change):
        assert detect(CaptureSignals(**{**signals_to_json(SCAN), **change})) is None

    def test_a_mixed_pdf_is_ambiguous(self):
        assert detect(CaptureSignals(pages=2, text_pages=1, raster_pages=1, median_dpi=200.0, page_short_mm=204,
                                     page_long_mm=291)) is None

    def test_a_text_page_missing_is_ambiguous(self):
        assert detect(CaptureSignals(pages=2, text_pages=1, raster_pages=0, page_short_mm=210,
                                     page_long_mm=297)) is None

    def test_no_signals_or_no_pages_decide_nothing(self):
        assert detect(None) is None
        assert detect(CaptureSignals()) is None

    def test_the_dpi_bounds_are_inclusive(self):
        for dpi in (SCAN_DPI_MIN, SCAN_DPI_MAX):
            assert detect(CaptureSignals(**{**signals_to_json(SCAN), "median_dpi": float(dpi)})) is Capture.SCAN


class TestSettle:
    def test_a_deterministic_answer_beats_the_model(self):
        assert settle(Capture.SCAN, Capture.DOCUMENT_PHOTO) == ("scan", CaptureSource.DETECTED.value)

    def test_the_model_decides_the_ambiguous(self):
        assert settle(None, Capture.SCREENSHOT) == ("screenshot", "automatic")

    def test_neither_is_no_capture(self):
        assert settle(None, None) == ("", "")


class TestModelAndCamera:
    @pytest.mark.parametrize("value,expected", [("scan", Capture.SCAN), (" Screenshot ", Capture.SCREENSHOT),
                                                ("document_photo", Capture.DOCUMENT_PHOTO),
                                                ("digital", Capture.DIGITAL), ("none", None), ("photo", None),
                                                (None, None), (3, None)])
    def test_model_capture(self, value, expected):
        assert model_capture(value) is expected

    def test_graph_camera_make_or_model_is_a_document_photo(self):
        assert camera_capture(DriveMediaFacts(camera_make="Synthetic")) is Capture.DOCUMENT_PHOTO
        assert camera_capture(DriveMediaFacts(camera_model="S2")) is Capture.DOCUMENT_PHOTO

    def test_a_taken_date_alone_is_no_camera(self):
        assert camera_capture(DriveMediaFacts(taken_at=datetime(2026, 1, 1, tzinfo=timezone.utc))) is None
        assert camera_capture(DriveMediaFacts()) is None

    def test_every_capture_has_a_label(self):
        assert set(CAPTURE_LABELS) == {c.value for c in Capture}


class TestWireForm:
    def test_round_trip(self):
        assert signals_from_json(signals_to_json(SCAN)) == SCAN

    @pytest.mark.parametrize("bad", [
        None, [], "x", {}, {**signals_to_json(SCAN), "extra": 1},
        {**signals_to_json(SCAN), "pages": -1}, {**signals_to_json(SCAN), "pages": True},
        {**signals_to_json(SCAN), "text_pages": 3}, {**signals_to_json(SCAN), "raster_pages": 3},
        {**signals_to_json(SCAN), "median_dpi": "200"}, {**signals_to_json(SCAN), "median_dpi": -1},
        {**signals_to_json(SCAN), "page_long_mm": 1e9}, {**signals_to_json(SCAN), "exif_camera": 1},
    ])
    def test_a_shape_that_is_not_the_signals_is_none(self, bad):
        assert signals_from_json(bad) is None
