"""
The capture signals of a PDF (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §26.3, D34), read inside the sandboxed parser
child (`drive_parser.child`) only: pypdf walks each page's content stream, never decodes an image, and a JPEG's EXIF
is read from its raw bytes. The rules that decide from the signals are pure (`src/domain/drive_capture.py`).

Per page (the first CAPTURE_PAGES_MAX): whether it draws text (a text layer, visible or an OCR layer in render mode
3); the images it draws, with the transformation in force (`q`/`Q`/`cm`, form XObjects followed down to
_FORM_DEPTH), each image's share of the page and its effective resolution. A page whose only image covers at least
FULL_PAGE_COVER of it is a full-page raster. A page whose only content is one image (no text, whatever its cover: a
phone photo on A4 with margins) has its JPEG's EXIF asked for a camera (Make or Model), from the raw stream bytes,
never decoded; a photo beside text is a figure of a digital report, not a photo of the document. The whole walk has
its own time budget (CAPTURE_BUDGET_S): out of time it raises TimeoutError and the caller keeps the text without signals.

Public interface:
    pdf_capture_signals(path) -> CaptureSignals          raises on a file pypdf cannot read, or out of time (the caller drops it)
    exif_names_camera(jpeg) -> bool
"""
from __future__ import annotations

import statistics
import time
from typing import Any, List, Optional, Tuple

from src.adapters.drive_parser.child import is_memory_failure
from src.domain.drive_capture import CaptureSignals
from src.domain.drive_index_params import CAPTURE_BUDGET_S, CAPTURE_PAGES_MAX, FULL_PAGE_COVER

_DEADLINE_EVERY = 256         # operations between two looks at the clock
_FORM_DEPTH = 3                 # form XObjects inside form XObjects followed this deep
_OPS_MAX = 200_000              # content-stream operations read per page (a hostile stream ends the page, not the file)
_EXIF_SCAN_BYTES = 65_536       # the JPEG head the EXIF segment is looked for in
_EXIF_SEGMENTS_MAX = 32
_IFD_ENTRIES_MAX = 512
_TAG_MAKE, _TAG_MODEL = 0x010F, 0x0110
_ASCII = 2
_TEXT_OPS = (b"Tj", b"TJ", b"'", b'"')
_MM_PER_PT = 25.4 / 72
Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _Image:
    """`jpeg`: the image's raw stream bytes when its only filter is DCTDecode (a JPEG's own bytes, never decoded), else
    None (any other filter chain is not decoded here: it could be a decompression bomb)."""
    __slots__ = ("px_w", "px_h", "ctm", "jpeg")

    def __init__(self, px_w: float, px_h: float, ctm: Matrix, jpeg: Optional[bytes]) -> None:
        self.px_w, self.px_h, self.ctm, self.jpeg = px_w, px_h, ctm, jpeg


class _Page:
    def __init__(self, deadline: float) -> None:
        self.text = False
        self.images: List[_Image] = []
        self.ops = 0
        self.deadline = deadline


def pdf_capture_signals(path: str) -> CaptureSignals:
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        return CaptureSignals()
    if reader.flattened_pages is None:
        try:
            reader.get_page(0)                    # the walked page tree, never /Count (as readers._page_count)
        except IndexError:
            return CaptureSignals()
    pages = list(reader.flattened_pages or ())[:CAPTURE_PAGES_MAX]
    deadline = time.monotonic() + CAPTURE_BUDGET_S
    text_pages = raster_pages = 0
    dpis: List[float] = []
    short_mm = long_mm = 0.0
    camera = False
    for page in pages:
        if time.monotonic() > deadline:
            raise TimeoutError("capture signals out of time")   # the caller drops them; the text outcome stays
        left, bottom, right, top = (float(v) for v in page.mediabox)
        width, height = abs(right - left), abs(top - bottom)
        short_mm = max(short_mm, min(width, height) * _MM_PER_PT)
        long_mm = max(long_mm, max(width, height) * _MM_PER_PT)
        seen = _Page(deadline)
        try:
            _walk_page(page, reader, seen)
            only = seen.images[0] if len(seen.images) == 1 else None
            # rule 1 reads a page whose only content is one image, whatever its cover (a phone photo on A4 with
            # margins); an image beside text or other images is a figure, not the document's capture
            if only is not None and not seen.text and only.jpeg is not None and exif_names_camera(only.jpeg):
                camera = True
        except TimeoutError:
            raise
        except Exception as exc:
            if is_memory_failure(exc):
                raise
            continue                              # an unreadable page draws nothing we can name: no rule holds
        text_pages += seen.text
        if only is None:
            continue
        if _cover(only.ctm, (min(left, right), min(bottom, top), max(left, right), max(bottom, top))) < FULL_PAGE_COVER:
            continue
        raster_pages += 1
        dpi = _dpi(only)
        if dpi > 0:
            dpis.append(dpi)
    return CaptureSignals(pages=len(pages), text_pages=text_pages, raster_pages=raster_pages,
                          median_dpi=round(float(statistics.median(dpis)), 2) if dpis else 0.0,
                          page_short_mm=round(short_mm, 2), page_long_mm=round(long_mm, 2), exif_camera=camera)


def exif_names_camera(jpeg: bytes) -> bool:
    """A JPEG whose EXIF (APP1, IFD0) holds a non-empty Make or Model. Anything malformed names none."""
    data = jpeg[:_EXIF_SCAN_BYTES]
    if not data.startswith(b"\xff\xd8"):
        return False
    i = 2
    for _ in range(_EXIF_SEGMENTS_MAX):
        if i + 4 > len(data) or data[i] != 0xFF:
            return False
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):                # end of image, start of scan: no EXIF before the pixels
            return False
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return False
        segment = data[i + 4:i + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            return _tiff_names_camera(segment[6:])
        i += 2 + length
    return False


# -- internals ------------------------------------------------------------------------------------------------------

def _walk_page(page: Any, reader: Any, seen: _Page) -> None:
    contents = page.get_contents()
    if contents is None:
        return
    from pypdf.generic import ContentStream

    _walk(ContentStream(contents, reader).operations, page.get("/Resources"), _IDENTITY, reader, seen, 0)


def _walk(operations: Any, resources: Any, ctm: Matrix, reader: Any, seen: _Page, depth: int) -> None:
    stack: List[Matrix] = []
    for operands, operator in operations:
        seen.ops += 1
        if seen.ops > _OPS_MAX:
            return
        if seen.ops % _DEADLINE_EVERY == 0 and time.monotonic() > seen.deadline:
            raise TimeoutError("capture signals out of time")
        if operator == b"q":
            stack.append(ctm)
        elif operator == b"Q":
            ctm = stack.pop() if stack else ctm
        elif operator == b"cm" and len(operands) == 6:
            ctm = _multiply(tuple(float(v) for v in operands), ctm)  # type: ignore[arg-type]
        elif operator in _TEXT_OPS:
            seen.text = seen.text or _shows_text(operator, operands)
        elif operator == b"INLINE IMAGE":
            # pypdf hands an inline image over as ONE dict {"settings": ..., "data": ...}, not an operand list
            inline = operands if isinstance(operands, dict) else {}
            settings = inline.get("settings") or {}
            px_w = _number(settings.get("/W", settings.get("/Width")))
            px_h = _number(settings.get("/H", settings.get("/Height")))
            data = inline.get("data")
            jpeg = bytes(data[:_EXIF_SCAN_BYTES]) if isinstance(data, (bytes, bytearray)) and \
                _only_dct(settings.get("/F", settings.get("/Filter"))) else None
            seen.images.append(_Image(px_w, px_h, ctm, jpeg))
        elif operator == b"Do" and operands:
            _draw(operands[0], resources, ctm, reader, seen, depth)


def _draw(name: Any, resources: Any, ctm: Matrix, reader: Any, seen: _Page, depth: int) -> None:
    found = _get(_get(resources, "/XObject"), name)
    if found is None:
        return
    xobject = found.get_object()
    subtype = xobject.get("/Subtype")
    if subtype == "/Image":
        seen.images.append(_Image(_number(xobject.get("/Width")), _number(xobject.get("/Height")), ctm,
                                  _raw_jpeg(xobject)))
    elif subtype == "/Form" and depth < _FORM_DEPTH:
        from pypdf.generic import ContentStream

        matrix = xobject.get("/Matrix")
        form_ctm = _multiply(tuple(float(v) for v in matrix), ctm) if matrix and len(matrix) == 6 else ctm  # type: ignore[arg-type]
        form_resources = xobject.get("/Resources") or resources
        _walk(ContentStream(xobject, reader).operations, form_resources, form_ctm, reader, seen, depth + 1)


def _get(container: Any, key: Any) -> Any:
    if container is None:
        return None
    container = container.get_object() if hasattr(container, "get_object") else container
    return container.get(key) if hasattr(container, "get") else None


def _number(value: Any) -> float:
    try:
        return float(value.get_object() if hasattr(value, "get_object") else value)
    except (TypeError, ValueError):
        return 0.0


def _shows_text(operator: bytes, operands: Any) -> bool:
    if operator == b"TJ":
        return bool(operands) and any(isinstance(v, (str, bytes)) and len(v) > 0 for v in operands[0])
    shown = operands[2] if operator == b'"' and len(operands) > 2 else (operands[0] if operands else "")
    return isinstance(shown, (str, bytes)) and len(shown) > 0


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    """m × n for PDF's row-vector matrices: a `cm` M makes the new CTM M × CTM."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2, e * a2 + f * c2 + e2,
            e * b2 + f * d2 + f2)


def _cover(ctm: Matrix, box: Tuple[float, float, float, float]) -> float:
    """The share of the page the image's unit square, transformed by `ctm` and clipped to the page, covers."""
    a, b, c, d, e, f = ctm
    xs = [e, a + e, c + e, a + c + e]
    ys = [f, b + f, d + f, b + d + f]
    left, bottom, right, top = box
    page_area = (right - left) * (top - bottom)
    if page_area <= 0:
        return 0.0
    width = max(0.0, min(max(xs), right) - max(min(xs), left))
    height = max(0.0, min(max(ys), top) - max(min(ys), bottom))
    return width * height / page_area


def _dpi(image: _Image) -> float:
    """Pixels per inch along the image's own axes (the transformed unit vectors), averaged over the two."""
    a, b, c, d, _, _ = image.ctm
    across, down = (a * a + b * b) ** 0.5 / 72, (c * c + d * d) ** 0.5 / 72
    if across <= 0 or down <= 0 or image.px_w <= 0 or image.px_h <= 0:
        return 0.0
    return (image.px_w / across + image.px_h / down) / 2


def _only_dct(filters: Any) -> bool:
    names = list(filters) if isinstance(filters, list) else [filters]
    return len(names) == 1 and names[0] in ("/DCTDecode", "/DCT")


def _raw_jpeg(xobject: Any) -> Optional[bytes]:
    """The head of an image XObject's raw stream when DCTDecode is its only filter: the JPEG's bytes, never decoded."""
    if not _only_dct(xobject.get("/Filter")):
        return None
    data = getattr(xobject, "_data", None)
    return bytes(data[:_EXIF_SCAN_BYTES]) if isinstance(data, (bytes, bytearray)) else None


def _tiff_names_camera(tiff: bytes) -> bool:
    order = tiff[:2]
    if order not in (b"II", b"MM") or len(tiff) < 8:
        return False
    endian = "little" if order == b"II" else "big"

    def u16(at: int) -> int:
        return int.from_bytes(tiff[at:at + 2], endian)

    def u32(at: int) -> int:
        return int.from_bytes(tiff[at:at + 4], endian)

    if u16(2) != 42:
        return False
    ifd = u32(4)
    if ifd + 2 > len(tiff):
        return False
    for k in range(min(u16(ifd), _IFD_ENTRIES_MAX)):
        entry = ifd + 2 + 12 * k
        if entry + 12 > len(tiff):
            return False
        tag, kind, count = u16(entry), u16(entry + 2), u32(entry + 4)
        if tag not in (_TAG_MAKE, _TAG_MODEL) or kind != _ASCII or count == 0:
            continue
        at = entry + 8 if count <= 4 else u32(entry + 8)
        value = tiff[at:at + count].rstrip(b"\x00").strip()
        if value:
            return True
    return False
