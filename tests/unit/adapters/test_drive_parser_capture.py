"""Parser@3's capture signals (USER_DRIVE_INDEXING_RFC §26.3, §26.8 S2): read inside the sandboxed child from the
committed fixtures (scripts/validation/drive_capture_fixtures.py), reported only for a PDF, and checked by the parent
like every other field the child writes."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapters.drive_parser.capture import exif_names_camera, pdf_capture_signals
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_capture import Capture, CaptureSignals, detect, signals_to_json
from src.domain.drive_extraction import PARSER, ExtractionStatus

from tests.unit.adapters.drive_extraction_fixtures import make_pdf

_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = _ROOT / "tests" / "fixtures" / "drive_capture"


def _signals(name):
    return pdf_capture_signals(str(FIXTURES / name))


class TestSignals:
    def test_a_text_only_pdf_is_digital(self):
        s = _signals("text_only.pdf")
        assert (s.pages, s.text_pages, s.raster_pages, s.exif_camera) == (2, 2, 0, False)
        assert detect(s) is Capture.DIGITAL

    def test_a_full_page_jpeg_at_200_dpi_without_exif_is_a_scan(self):
        s = _signals("scan_200dpi.pdf")
        assert (s.pages, s.text_pages, s.raster_pages, s.exif_camera) == (2, 0, 2, False)
        assert s.median_dpi == pytest.approx(200, abs=1)
        assert s.page_short_mm == pytest.approx(139.7, abs=0.5) and s.page_long_mm == pytest.approx(190.5, abs=0.5)
        assert detect(s) is Capture.SCAN

    def test_the_same_with_an_ocr_text_layer_is_still_a_scan(self):
        s = _signals("scan_200dpi_ocr.pdf")
        assert (s.pages, s.text_pages, s.raster_pages) == (2, 2, 2)
        assert detect(s) is Capture.SCAN                           # Δ57

    def test_a_jpeg_naming_a_camera_is_a_document_photo(self):
        s = _signals("phone_photo.pdf")
        assert s.exif_camera is True and s.raster_pages == 1
        assert detect(s) is Capture.DOCUMENT_PHOTO

    def test_a_mixed_pdf_is_ambiguous(self):
        s = _signals("mixed.pdf")
        assert (s.pages, s.text_pages, s.raster_pages) == (2, 1, 1)
        assert detect(s) is None

    def test_a_pdf_without_text_or_image(self, tmp_path):
        s = pdf_capture_signals(str(make_pdf(tmp_path / "blank.pdf", ["", ""])))
        assert (s.pages, s.text_pages, s.raster_pages) == (2, 0, 0)
        assert detect(s) is None


class TestExif:
    def test_make_and_model_name_a_camera(self):
        assert exif_names_camera((FIXTURES / "phone_photo.jpg").read_bytes()) is True

    def test_resolution_alone_names_none(self):
        # the scanner's EXIF of §26.1: resolution and nothing else
        from pypdf import PdfReader

        page = PdfReader(str(FIXTURES / "scan_200dpi.pdf")).pages[0]
        jpeg = page["/Resources"]["/XObject"]["/Im0"].get_object().get_data()
        assert jpeg[:2] == b"\xff\xd8" and exif_names_camera(jpeg) is False

    @pytest.mark.parametrize("data", [b"", b"\xff\xd8", b"not a jpeg", b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00II*\x00",
                                      b"\xff\xd8\xff\xe1\xff\xffExif\x00\x00MM\x00*\x00\x00\x00\x08\xff\xff"])
    def test_garbage_names_none(self, data):
        assert exif_names_camera(data) is False


def _child(tmp_path, source: Path, kind: str) -> dict:
    request = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
               "file_bytes": 64 * 1024 ** 2, "op": "extract", "path": str(source), "kind": kind}
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=json.dumps(request).encode(), cwd=_ROOT, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))


class TestChild:
    def test_the_child_reports_the_signals_of_a_pdf(self, tmp_path):
        result = _child(tmp_path, FIXTURES / "scan_200dpi_ocr.pdf", "pdf")
        assert result["status"] == "text"
        assert result["capture_signals"]["raster_pages"] == 2 and result["capture_signals"]["text_pages"] == 2

    def test_a_scan_without_text_still_reports_them(self, tmp_path):
        result = _child(tmp_path, FIXTURES / "scan_200dpi.pdf", "pdf")
        assert result["status"] == "no_text" and result["capture_signals"]["raster_pages"] == 2

    def test_other_kinds_carry_none(self, tmp_path):
        source = tmp_path / "a.txt"
        source.write_text("plain text", encoding="utf-8")
        assert "capture_signals" not in _child(tmp_path, source, "text")


class TestParent:
    def test_parser_version_is_3(self):
        assert SubprocessContentExtractor.VERSION == 3
        assert SubprocessContentExtractor().stamp == f"{PARSER}@3"

    def test_the_parent_passes_valid_signals(self):
        signals = CaptureSignals(pages=1, text_pages=0, raster_pages=1, median_dpi=200.0, page_short_mm=140.0,
                                 page_long_mm=190.0)
        outcome = SubprocessContentExtractor()._from_result({
            "status": "no_text", "text": "", "pages_total": 1, "truncated": False,
            "capture_signals": signals_to_json(signals)})
        assert outcome.status is ExtractionStatus.NO_TEXT and outcome.capture_signals == signals

    def test_the_parent_drops_a_bad_shape_without_failing_the_file(self):
        outcome = SubprocessContentExtractor()._from_result({
            "status": "text", "text": "x", "pages_total": 1, "truncated": False,
            "capture_signals": {"pages": "lots"}})
        assert outcome.status is ExtractionStatus.TEXT and outcome.capture_signals is None

    def test_a_result_without_signals_has_none(self):
        outcome = SubprocessContentExtractor()._from_result({"status": "text", "text": "x", "pages_total": 0,
                                                             "truncated": False})
        assert outcome.capture_signals is None


class TestInlineImagesAndCameraPages:
    """Capture walk review fixes: pypdf's inline-image operand is one dict; a camera JPEG on a page with margins."""

    def test_a_digital_page_with_an_inline_logo_stays_digital(self):
        s = _signals("inline_logo.pdf")
        assert (s.pages, s.text_pages, s.raster_pages, s.exif_camera) == (1, 1, 0, False)
        assert detect(s) is Capture.DIGITAL

    def test_a_full_page_inline_image_is_a_raster(self):
        s = _signals("inline_full_page.pdf")
        assert (s.pages, s.text_pages, s.raster_pages) == (1, 0, 1) and s.median_dpi > 0

    def test_a_phone_photo_with_margins_names_its_camera_without_being_a_raster(self):
        s = _signals("phone_on_a4.pdf")
        assert (s.pages, s.text_pages, s.raster_pages, s.exif_camera) == (1, 0, 0, True)
        assert detect(s) is Capture.DOCUMENT_PHOTO

    def test_a_photo_beside_text_is_a_figure_not_a_capture(self, monkeypatch):
        import src.adapters.drive_parser.capture as cap
        monkeypatch.setattr(cap, "exif_names_camera", lambda jpeg: True)
        assert _signals("inline_logo.pdf").exif_camera is False        # text on the page: the image is a figure

    def test_out_of_time_raises_and_the_caller_keeps_the_text(self, monkeypatch):
        import src.adapters.drive_parser.capture as cap
        monkeypatch.setattr(cap, "CAPTURE_BUDGET_S", -1.0)
        with pytest.raises(TimeoutError):
            _signals("text_only.pdf")

    def test_the_reader_drops_signals_out_of_time_but_keeps_the_text(self, monkeypatch):
        import src.adapters.drive_parser.readers as readers
        monkeypatch.setattr(readers, "pdf_capture_signals", lambda path: (_ for _ in ()).throw(TimeoutError("x")))
        assert readers._capture_signals(str(FIXTURES / "text_only.pdf")) is None

    def test_one_page_past_the_deadline_stops_the_walk(self, monkeypatch):
        import src.adapters.drive_parser.capture as cap
        times = iter([0.0, 0.0, 99.0, 99.0, 99.0])
        monkeypatch.setattr(cap.time, "monotonic", lambda: next(times, 99.0))
        with pytest.raises(TimeoutError):
            _signals("text_only.pdf")                                  # two pages: the second starts too late

    def test_a_non_dct_filter_is_never_decoded_for_exif(self):
        import src.adapters.drive_parser.capture as cap
        assert cap._only_dct("/DCTDecode") and cap._only_dct(["/DCT"])
        assert not cap._only_dct(["/FlateDecode", "/DCTDecode"]) and not cap._only_dct(None)
