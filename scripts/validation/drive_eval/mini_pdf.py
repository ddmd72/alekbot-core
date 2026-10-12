"""
A one-page PDF with a real text layer, written byte for byte (no library, no timestamp), for the eval's bulk folders:
600 invoices are rendered from a template when the fixture drive serves them, so they are never committed. Latin-1
text only (Helvetica, WinAnsiEncoding). Deterministic: the same lines give the same bytes.
"""
from __future__ import annotations

from typing import List, Sequence


def _escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def text_pdf(lines: Sequence[str], font_size: int = 11) -> bytes:
    """One A4 page, one line of text per item, top to bottom."""
    leading = font_size + 4
    ops = [f"BT /F1 {font_size} Tf {leading} TL 56 790 Td"]
    for i, line in enumerate(lines):
        ops.append(("" if i == 0 else "T* ") + f"({_escape(line)}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
