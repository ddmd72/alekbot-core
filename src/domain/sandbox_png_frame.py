"""
render.png request body framing (S2): `u32-be width | html`.

Pure bytes, stdlib only. The caller's client frames the body; the engine child splits it. The server itself never
does either (it streams the body to a file untouched).
"""
from __future__ import annotations

import struct
from typing import Tuple

PNG_WIDTH_MIN = 16
PNG_WIDTH_MAX = 2048

_WIDTH = struct.Struct(">I")


class BadPngBody(ValueError):
    """The width is out of range or not an int, or the body has no width header."""


def frame_png_body(width: int, html: bytes) -> bytes:
    if isinstance(width, bool) or not isinstance(width, int):
        raise BadPngBody("width must be an int")
    if not PNG_WIDTH_MIN <= width <= PNG_WIDTH_MAX:
        raise BadPngBody(f"width must be {PNG_WIDTH_MIN}..{PNG_WIDTH_MAX}")
    return _WIDTH.pack(width) + html


def split_png_body(body: bytes) -> Tuple[int, bytes]:
    if len(body) < _WIDTH.size:
        raise BadPngBody("body has no width header")
    (width,) = _WIDTH.unpack_from(body)
    if not PNG_WIDTH_MIN <= width <= PNG_WIDTH_MAX:
        raise BadPngBody(f"width must be {PNG_WIDTH_MIN}..{PNG_WIDTH_MAX}")
    return width, body[_WIDTH.size:]
