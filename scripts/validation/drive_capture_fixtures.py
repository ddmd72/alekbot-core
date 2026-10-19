"""
Generate the capture fixtures (USER_DRIVE_INDEXING_RFC §26.3, §26.8 S2) into tests/fixtures/drive_capture/.

    venv/bin/python -m scripts.validation.drive_capture_fixtures

The output is committed, not generated at test time (as make_fixtures.py does for the eval): Pillow's JPEG encoder may
change its bytes between versions, and the parser tests read the files as they are. Re-running this script is a
deliberate fixture change. Every page is synthetic: grey bars and boxes, no person, number or document of anyone.

The files, each built as a scanner or a phone would build it (§26.1 item 2):
  text_only.pdf          two pages with a text layer and no image (a digital document)
  scan_200dpi.pdf        two pages, each one full-page grey JPEG at 200 dpi on a page the size of the image (A5, inside
                         a scanner glass), EXIF with only the resolution: no Make, no Model; no text layer
  scan_200dpi_ocr.pdf    the same with an invisible OCR text layer (render mode 3) over each page image (Δ57)
  phone_photo.jpg        a JPEG whose EXIF names a camera (Make and Model)
  phone_photo.pdf        that JPEG as a full-page image on one page
  mixed.pdf              page 1 a text layer, page 2 a full-page JPEG: ambiguous, the model decides
  inline_logo.pdf        one A4 page with a text layer and a small inline image (BI/ID/EI): stays digital
  inline_full_page.pdf   one page whose only content is a full-page inline image: a raster
  phone_on_a4.pdf        the phone JPEG placed on an A4 page with margins, no text: EXIF still names the camera
Needs Pillow.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "drive_capture"
DPI = 200
PAGE_PX = (1100, 1500)          # 139.7 x 190.5 mm at 200 dpi: on any scanner glass
_EXIF_MAKE, _EXIF_MODEL, _EXIF_X_RES, _EXIF_Y_RES, _EXIF_RES_UNIT = 0x010F, 0x0110, 0x011A, 0x011B, 0x0128


def page_jpeg(seed: int, *, camera: bool) -> bytes:
    """A grey page with bars for lines of text; EXIF with the resolution only, or with a camera's Make and Model."""
    from PIL import Image, ImageDraw

    width, height = PAGE_PX
    image = Image.new("L", (width, height), 245)
    draw = ImageDraw.Draw(image)
    for row in range(14):
        top = 120 + row * 90
        draw.rectangle((100, top, 100 + 300 + ((row * 37 + seed * 53) % 600), top + 22), fill=60)
    draw.rectangle((90, 90, width - 90, height - 90), outline=120, width=3)
    exif = Image.Exif()
    exif[_EXIF_X_RES] = DPI
    exif[_EXIF_Y_RES] = DPI
    exif[_EXIF_RES_UNIT] = 2
    if camera:
        exif[_EXIF_MAKE] = "SyntheticPhone"
        exif[_EXIF_MODEL] = "Model S2"
    out = io.BytesIO()
    image.save(out, "JPEG", quality=30, dpi=(DPI, DPI), exif=exif.tobytes())
    return out.getvalue()


def _pdf(objects: List[bytes]) -> bytes:
    """Objects numbered from 1 in order; object 1 is the catalog."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def pdf(pages: Sequence[Tuple[Optional[bytes], Optional[str], bool]]) -> bytes:
    """`pages`: (jpeg or None, text or None, text visible). A JPEG fills its page, the page is the image's size at
    DPI; a page without one is A4. Text is drawn with Helvetica, visible or invisible (render mode 3, an OCR layer)."""
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for jpeg, text, visible in pages:
        resources = [b"/Font << /F1 3 0 R >>"]
        ops: List[bytes] = []
        if jpeg is not None:
            px_w, px_h = PAGE_PX
            width, height = px_w * 72 / DPI, px_h * 72 / DPI
            objects.append(_stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                                   b"/BitsPerComponent 8 /Filter /DCTDecode" % (px_w, px_h), jpeg))
            resources.append(b"/XObject << /Im0 %d 0 R >>" % len(objects))
            ops.append(b"q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (width, height))
        else:
            width, height = 595.28, 841.89
        if text:
            mode = b"0" if visible else b"3"
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
            ops.append(b"BT " + mode + b" Tr /F1 12 Tf 72 %.2f Td (" % (height - 72) + escaped + b") Tj ET")
        objects.append(_stream(b"", b"\n".join(ops)))
        content = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources << %s >> /Contents %d 0 R >>"
                       % (width, height, b" ".join(resources), content))
        kids.append(b"%d 0 R" % len(objects))
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(pages)
    return _pdf(objects)


def single_page_pdf(media: Tuple[float, float], content: bytes, xobjects: Sequence[bytes] = ()) -> bytes:
    """One page of `media` points: `content` is its stream; each of `xobjects` is an image stream named /Im0, /Im1..."""
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>",
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", _stream(b"", content)]
    names = []
    for number, body in enumerate(xobjects):
        objects.append(body)
        names.append(b"/Im%d %d 0 R" % (number, len(objects)))
    objects.insert(4, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Resources << /Font << /F1 3 0 R >> "
                      b"/XObject << %s >> >> /Contents 4 0 R >>" % (media[0], media[1], b" ".join(names)))
    # the page object was inserted at 5: shift the XObject numbers that followed it
    fixed = []
    for n, name in enumerate(names):
        fixed.append(b"/Im%d %d 0 R" % (n, 6 + n))
    objects[4] = objects[4].replace(b" ".join(names), b" ".join(fixed))
    return _pdf(objects)


def _inline_image(px: int, scale_pt: float, x: float, y: float) -> bytes:
    """A gray px x px inline image (raw bytes, no filter) drawn scale_pt points wide and high at (x, y)."""
    data = bytes((i * 7) % 256 for i in range(px * px))
    return (b"q %.2f 0 0 %.2f %.2f %.2f cm\nBI /W %d /H %d /CS /G /BPC 8\nID " % (scale_pt, scale_pt, x, y, px, px)
            + data + b"\nEI Q")


_LINE = "Synthetic sample page {n}: grey bars only, no real document"


def build() -> dict:
    scans = [page_jpeg(1, camera=False), page_jpeg(2, camera=False)]
    phone = page_jpeg(3, camera=True)
    a4 = (595.28, 841.89)
    text_ops = b"BT /F1 12 Tf 72 760 Td (" + _LINE.format(n=1).encode() + b") Tj ET\n"
    phone_stream = _stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                           b"/BitsPerComponent 8 /Filter /DCTDecode" % PAGE_PX, phone)
    return {
        "inline_logo.pdf": single_page_pdf(a4, text_ops + _inline_image(8, 40, 480, 760)),
        "inline_full_page.pdf": single_page_pdf((200.0, 200.0), _inline_image(8, 200, 0, 0)),
        "phone_on_a4.pdf": single_page_pdf(a4, b"q 400 0 0 540 90 150 cm /Im0 Do Q", [phone_stream]),
        "text_only.pdf": pdf([(None, _LINE.format(n=1), True), (None, _LINE.format(n=2), True)]),
        "scan_200dpi.pdf": pdf([(scans[0], None, False), (scans[1], None, False)]),
        "scan_200dpi_ocr.pdf": pdf([(scans[0], _LINE.format(n=1), False), (scans[1], _LINE.format(n=2), False)]),
        "phone_photo.jpg": phone,
        "phone_photo.pdf": pdf([(phone, None, False)]),
        "mixed.pdf": pdf([(None, _LINE.format(n=1), True), (scans[0], None, False)]),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in build().items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
