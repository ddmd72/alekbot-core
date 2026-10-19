"""Capture in the tick (USER_DRIVE_INDEXING_RFC §26.3, D34, Δ57): the parser's signals survive the hop to the reader and
a deterministic answer beats the model's; an image read as text takes the camera facet, the reader's word, then
triage's; a photograph and an office file have none."""
from dataclasses import replace
from typing import Optional, Sequence

import pytest

from src.domain.drive_capture import CaptureSignals
from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus, TriageReply, TriageResultV2
from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus, TriageImage
from src.domain.drive_index import TriageVerdict
from src.ports.image_triage_port import ImageTriagePort

from .drive_content_world import ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")

SCANNER = CaptureSignals(pages=2, text_pages=0, raster_pages=2, median_dpi=200.0, page_short_mm=140.0,
                         page_long_mm=190.0)
MIXED = CaptureSignals(pages=2, text_pages=1, raster_pages=1, median_dpi=200.0, page_short_mm=210.0,
                       page_long_mm=297.0)
DIGITAL = CaptureSignals(pages=2, text_pages=2, raster_pages=0, page_short_mm=210.0, page_long_mm=297.0)


class CapturingTriage(ImageTriagePort):
    """Every image is a text image, with the capture triage says (scripted by item id)."""

    def __init__(self, captures: Optional[dict] = None, photos: Sequence[str] = ()) -> None:
        self.captures = captures or {}
        self.photos = set(photos)

    async def triage(self, user_id, account_id, images: Sequence[TriageImage]) -> TriageReply:
        results = []
        for image in images:
            if image.item_id in self.photos:
                results.append(TriageResultV2(item_id=image.item_id, verdict=TriageVerdict.PHOTO,
                                              photo=PhotoDescription(description="a dog"), capture="scan"))
            else:
                results.append(TriageResultV2(item_id=image.item_id, verdict=TriageVerdict.TEXT,
                                              capture=self.captures.get(image.item_id, "")))
        return TriageReply(status=ModelReplyStatus.OK, results=tuple(results), model="fake", usd=0.0, model_calls=1)


def _text(signals=None, **kw):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30, "lease"), pages_total=2,
                             capture_signals=signals, **kw)


def _read(capture="", **kw):
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=prose(30, "receipt"), pages_total=1, pages_sent=1,
                             model_calls=1, capture=capture, capture_source="automatic" if capture else "", **kw)


@pytest.fixture
def w():
    return ContentWorld()


async def test_a_parsed_scan_is_a_detected_scan(w):
    w.parser.outcomes["a.pdf"] = _text(SCANNER)
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert (rec.capture, rec.capture_source) == ("scan", "detected") and w.reader.requests == []


async def test_a_parsed_digital_pdf_is_digital(w):
    w.parser.outcomes["a.pdf"] = _text(DIGITAL)
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("digital", "detected")


async def test_an_ambiguous_pdf_no_model_read_has_none(w):
    w.parser.outcomes["a.pdf"] = _text(MIXED)
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("", "")


async def test_the_parsers_signals_outlive_the_hop_to_the_reader_and_win(w):
    w.parser.outcomes["a.pdf"] = replace(_text(SCANNER), status=ExtractionStatus.NO_TEXT, text="")
    w.reader.outcomes["a.pdf"] = _read("screenshot")
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    rec = w.rec(doc)
    assert rec.extracted_by == "reader@1" and (rec.capture, rec.capture_source) == ("scan", "detected")


async def test_the_reader_decides_what_the_parser_left_ambiguous(w):
    w.parser.outcomes["a.pdf"] = replace(_text(MIXED), status=ExtractionStatus.NO_TEXT, text="")
    w.reader.outcomes["a.pdf"] = _read("document_photo")
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("document_photo", "automatic")


async def test_a_pdf_the_parser_sent_no_signals_for_takes_the_readers_word(w):
    w.parser.outcomes["a.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=1)
    w.reader.outcomes["a.pdf"] = _read("scan")
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("scan", "automatic")


async def test_a_text_image_with_a_camera_facet_is_a_detected_document_photo(w):
    img = w.add_image("page.jpg", camera=True)
    w.triage = CapturingTriage({img: "screenshot"})
    w.svc._triage = w.triage
    w.reader.outcomes["page.jpg"] = _read("scan")
    await w.settle()
    assert (w.rec(img).capture, w.rec(img).capture_source) == ("document_photo", "detected")


async def test_a_text_image_takes_the_readers_word_over_triages(w):
    img = w.add_image("page.jpg")
    w.svc._triage = CapturingTriage({img: "screenshot"})
    w.reader.outcomes["page.jpg"] = _read("scan")
    await w.settle()
    assert (w.rec(img).capture, w.rec(img).capture_source) == ("scan", "automatic")


async def test_a_text_image_the_reader_said_nothing_of_keeps_triages(w):
    img = w.add_image("page.jpg")
    w.svc._triage = CapturingTriage({img: "screenshot"})
    w.reader.outcomes["page.jpg"] = _read("")
    await w.settle()
    assert (w.rec(img).capture, w.rec(img).capture_source) == ("screenshot", "automatic")


async def test_triages_capture_waits_on_the_record_for_the_reader(w):
    img = w.add_image("page.jpg")
    w.svc._triage = CapturingTriage({img: "screenshot"})
    w.reader.unavailable = True
    await w.tick()
    rec = w.rec(img)
    assert rec.triage_verdict is TriageVerdict.TEXT and (rec.capture, rec.capture_source) == ("screenshot", "automatic")


async def test_a_photograph_has_no_capture_whatever_triage_says(w):
    img = w.add_image("dog.jpg", camera=True)
    w.svc._triage = CapturingTriage(photos=[img])
    await w.settle()
    rec = w.rec(img)
    assert rec.triage_verdict is TriageVerdict.PHOTO and (rec.capture, rec.capture_source) == ("", "")


@pytest.mark.parametrize("name", ["a.docx", "a.txt", "a.md"])
async def test_a_non_pdf_text_file_is_digital_detected(w, name):
    doc = w.add_doc(name, prose(30, "lease"))
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("digital", "detected")


@pytest.mark.parametrize("status", [ExtractionStatus.ENCRYPTED, ExtractionStatus.UNSUPPORTED,
                                    ExtractionStatus.TOO_LARGE])
async def test_a_file_the_parser_could_not_read_has_no_capture(w, status):
    w.parser.outcomes["a.docx"] = ExtractionOutcome(status=status)
    doc = w.add_doc("a.docx", "")
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("", "")


async def test_an_edit_that_leaves_no_text_clears_the_capture(w):
    w.parser.outcomes["a.pdf"] = _text(SCANNER)
    doc = w.add_doc("a.pdf", "")
    await w.settle()
    assert w.rec(doc).capture == "scan"
    w.parser.outcomes["a.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.reader.outcomes["a.pdf"] = ExtractionOutcome(status=ExtractionStatus.NO_TEXT, pages_total=2)
    w.drive.edit(doc, b"%PDF-1.7\nx", modified_at=w.clock.now)
    w.clock.advance(3600)
    await w.settle()
    assert (w.rec(doc).capture, w.rec(doc).capture_source) == ("", "")
