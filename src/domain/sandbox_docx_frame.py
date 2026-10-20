"""
render.docx request body framing (plan G1): `u32-be script_length | script | spec`.

Pure bytes, stdlib only. The caller's client frames the body; the engine child splits it. The server itself never
does either (it streams the body to a file untouched).
"""
from __future__ import annotations

import struct
from typing import Tuple

_LENGTH = struct.Struct(">I")


class BadBody(ValueError):
    """The framed body is shorter than its header says (or has no header)."""


def frame_body(script: bytes, spec: bytes) -> bytes:
    return _LENGTH.pack(len(script)) + script + spec


def split_body(body: bytes) -> Tuple[bytes, bytes]:
    if len(body) < _LENGTH.size:
        raise BadBody("body has no length header")
    (length,) = _LENGTH.unpack_from(body)
    end = _LENGTH.size + length
    if end > len(body):
        raise BadBody("script length runs past the body")
    return body[_LENGTH.size:end], body[end:]
