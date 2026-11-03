"""
`parse.pdf_windows` response body framing: `u32-be count | (u32-be length | bytes) x count`.

Pure bytes, stdlib only. The engine streams the frame to its output file with `frame_prefix` and `window_prefix` (it
never holds all windows in memory); the caller's client splits the body with `split_windows`. The server itself never
does either.
"""
from __future__ import annotations

import struct
from typing import List, Sequence

_LENGTH = struct.Struct(">I")


class BadFrame(ValueError):
    """The framed body is short, longer than its header says, or its count does not match its content."""


def frame_prefix(count: int) -> bytes:
    """The frame header: how many windows follow."""
    return _LENGTH.pack(count)


def window_prefix(length: int) -> bytes:
    """The header of one window: its byte length."""
    return _LENGTH.pack(length)


def frame_windows(windows: Sequence[bytes]) -> bytes:
    """The whole frame in memory (tests and small callers; the engine streams instead)."""
    return frame_prefix(len(windows)) + b"".join(window_prefix(len(w)) + w for w in windows)


def split_windows(body: bytes) -> List[bytes]:
    if len(body) < _LENGTH.size:
        raise BadFrame("frame has no count header")
    (count,) = _LENGTH.unpack_from(body)
    offset = _LENGTH.size
    windows: List[bytes] = []
    for _ in range(count):
        if offset + _LENGTH.size > len(body):
            raise BadFrame("fewer windows than the count says")
        (length,) = _LENGTH.unpack_from(body, offset)
        offset += _LENGTH.size
        end = offset + length
        if end > len(body):
            raise BadFrame("window length runs past the frame")
        windows.append(body[offset:end])
        offset = end
    if offset != len(body):
        raise BadFrame("trailing bytes after the last window")
    return windows
