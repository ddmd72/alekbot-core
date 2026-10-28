"""
Drive capture — how a text-bearing file was made: a scan, a photo of a document, a screenshot or a digital file
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §26.3, D34, Δ57, Δ58). Pure: stdlib only, no I/O.

Deterministic first: the sandboxed parser child reads a PDF's pages with pypdf and reports `CaptureSignals`; the rules
below decide from them when they are unambiguous (`detected`). The reader and triage models decide the rest
(`automatic`). A deterministic answer wins whenever one exists. A photograph (triage verdict `photo`) gets no capture:
it is a photo already, by its `k:` words (Δ57).

Rules, in order (`detect`):
  1. EXIF Make or Model in the JPEG a page is made of, or Graph's camera facet on an image read as text
     -> document_photo;
  2. every page one full-page raster (at least FULL_PAGE_COVER of the page), an effective resolution within
     SCAN_DPI_MIN..SCAN_DPI_MAX, no page larger than a scanner glass (SCAN_GLASS_MAX_MM) -> scan, with or without a
     text layer: a scanner's searchable PDF puts an OCR layer over the page images (Δ57);
  3. a text layer on every page and no full-page raster -> digital;
  4. anything else is ambiguous: the model decides, when one reads the file.

Public interface:
    Capture, CaptureSource                      the values (`DriveIndexRecord.capture`, `.capture_source` hold them)
    CaptureSignals                              what the parser child reports for a PDF
    signals_to_json(signals) -> dict, signals_from_json(value) -> CaptureSignals | None   the child's wire form
    detect(signals) -> Capture | None           rules 1-3 over a PDF's signals
    camera_capture(facets) -> Capture | None    rule 1 for an image: Graph's camera facet
    model_capture(value) -> Capture | None      a model's answer, or None when it is not one of the values
    settle(detected, model) -> (capture, source)   the deterministic answer wins; ("", "") when neither has one
    file_capture(kind, ...) -> (capture, source)   a file's capture from everything known; non-PDF, non-image kinds are digital (Δ79)
    CAPTURE_LABELS                              capture -> the words the card and the rendered line use
    IMAGE_TEXT_CAPTURES, born_digital(capture)  text read from an image vs software-made text (§28 OQ-A2)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from .drive_index_params import SCAN_DPI_MAX, SCAN_DPI_MIN, SCAN_GLASS_MAX_MM


class Capture(str, Enum):
    SCAN = "scan"
    DOCUMENT_PHOTO = "document_photo"
    SCREENSHOT = "screenshot"
    DIGITAL = "digital"


class CaptureSource(str, Enum):
    DETECTED = "detected"     # decided by code from the file's own structure: keyed `cp:` (exact, Δ58)
    AUTOMATIC = "automatic"   # said by a model: keyed `cpa:` (automatic, Δ58)


# The card text and the rendered `capture:` line (§26.3).
CAPTURE_LABELS: Mapping[str, str] = {
    Capture.SCAN.value: "scan", Capture.DOCUMENT_PHOTO.value: "photo of a document",
    Capture.SCREENSHOT.value: "screenshot", Capture.DIGITAL.value: "digital",
}


# §28 OQ-A2: the text is a reading of an image. `_SCAN_CAPTURES` in drive_operations (R_SCAN) stays apart.
IMAGE_TEXT_CAPTURES = frozenset({Capture.SCAN.value, Capture.DOCUMENT_PHOTO.value, Capture.SCREENSHOT.value})


def born_digital(capture: str) -> bool:
    """The text is software-made (§28 OQ-A2); an image reading and an unknown capture ("") are not."""
    return capture == Capture.DIGITAL.value


@dataclass(frozen=True)
class CaptureSignals:
    """A PDF's capture signals, from the first CAPTURE_PAGES_MAX pages (`pages`). `raster_pages`: pages whose only
    image is one raster covering at least FULL_PAGE_COVER of the page; `text_pages`: pages that draw text (a text
    layer, visible or an OCR layer); `median_dpi`: the effective resolution of those rasters (0 without one);
    `page_short_mm` / `page_long_mm`: the largest page inspected; `exif_camera`: a page whose only content is one
    image is a JPEG whose EXIF names a camera (Make or Model)."""
    pages: int = 0
    text_pages: int = 0
    raster_pages: int = 0
    median_dpi: float = 0.0
    page_short_mm: float = 0.0
    page_long_mm: float = 0.0
    exif_camera: bool = False


_COUNTS = ("pages", "text_pages", "raster_pages")
_MEASURES = ("median_dpi", "page_short_mm", "page_long_mm")
_MEASURE_MAX = 1e7


def signals_to_json(signals: CaptureSignals) -> Dict[str, Any]:
    return asdict(signals)


def signals_from_json(value: Any) -> Optional[CaptureSignals]:
    """The child's report, checked like every other field it writes: a shape that is not CaptureSignals is no
    signals (the model decides), never an error of the file."""
    if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(CaptureSignals)}:
        return None
    counts = [value[name] for name in _COUNTS]
    measures = [value[name] for name in _MEASURES]
    camera = value["exif_camera"]
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        return None
    if any(isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m < _MEASURE_MAX for m in measures):
        return None
    if not isinstance(camera, bool):
        return None
    pages, text_pages, raster_pages = counts
    if text_pages > pages or raster_pages > pages:
        return None
    dpi, short_mm, long_mm = (float(m) for m in measures)
    return CaptureSignals(pages=pages, text_pages=text_pages, raster_pages=raster_pages, median_dpi=dpi,
                          page_short_mm=short_mm, page_long_mm=long_mm, exif_camera=camera)


def _on_glass(short_mm: float, long_mm: float) -> bool:
    glass_short, glass_long = SCAN_GLASS_MAX_MM
    return 0 < short_mm <= glass_short and 0 < long_mm <= glass_long


def detect(signals: Optional[CaptureSignals]) -> Optional[Capture]:
    """Rules 1-3 (module docstring) over a PDF's signals; None when they are ambiguous or absent."""
    if signals is None or signals.pages <= 0:
        return None
    if signals.exif_camera:
        return Capture.DOCUMENT_PHOTO
    if (signals.raster_pages == signals.pages and SCAN_DPI_MIN <= signals.median_dpi <= SCAN_DPI_MAX
            and _on_glass(signals.page_short_mm, signals.page_long_mm)):
        return Capture.SCAN
    if signals.text_pages == signals.pages and signals.raster_pages == 0:
        return Capture.DIGITAL
    return None


def camera_capture(facets: Mapping[str, Any]) -> Optional[Capture]:
    """Rule 1 for an image read as text: Graph's `photo.cameraMake` or `photo.cameraModel` (the EXIF the provider
    read). A taken date alone is no camera: a screenshot can carry one."""
    photo = facets.get("photo") or {}
    if isinstance(photo, Mapping) and (photo.get("cameraMake") or photo.get("cameraModel")):
        return Capture.DOCUMENT_PHOTO
    return None


def model_capture(value: Any) -> Optional[Capture]:
    """A model's capture answer; anything outside the four values is no answer (the transcription never fails over
    it, like the brief's date)."""
    if not isinstance(value, str):
        return None
    try:
        return Capture(value.strip().lower())
    except ValueError:
        return None


def settle(detected: Optional[Capture], model: Optional[Capture]) -> Tuple[str, str]:
    """(capture, source) for the record: the deterministic answer wins whenever one exists (§26.3)."""
    if detected is not None:
        return detected.value, CaptureSource.DETECTED.value
    if model is not None:
        return model.value, CaptureSource.AUTOMATIC.value
    return "", ""


def file_capture(kind: str, *, signals: Optional[CaptureSignals] = None, facets: Optional[Mapping[str, Any]] = None,
                 model: str = "", triaged: str = "") -> Tuple[str, str]:
    """(capture, source) of a text-bearing file (§26.3, Δ79). A PDF has the parser's signals, then the reader's word
    (`model`); an image read as text has Graph's camera facet, then the reader's word, then triage's (`triaged`).
    Any other kind the parser reads (office files, plain text, HTML, mail, ...) is software-made by definition:
    `digital`, `detected`."""
    if kind == "pdf":
        return settle(detect(signals), model_capture(model))
    if kind == "image":
        return settle(camera_capture(facets or {}), model_capture(model) or model_capture(triaged))
    return Capture.DIGITAL.value, CaptureSource.DETECTED.value
