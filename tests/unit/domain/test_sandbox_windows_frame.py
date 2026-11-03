"""parse.pdf_windows response framing: `u32-be count | (u32-be length | bytes) x count`."""
import struct

import pytest

from src.domain.sandbox_windows_frame import BadFrame, frame_prefix, frame_windows, split_windows, window_prefix


def test_round_trip():
    windows = [b"%PDF-a", b"", b"%PDF-" + b"x" * 1000]
    assert split_windows(frame_windows(windows)) == windows


def test_prefixes_are_big_endian_u32():
    assert frame_prefix(3) == struct.pack(">I", 3) and window_prefix(258) == b"\x00\x00\x01\x02"


def test_streamed_prefixes_build_the_same_frame():
    assert frame_prefix(2) + window_prefix(1) + b"a" + window_prefix(2) + b"bc" == frame_windows([b"a", b"bc"])


def test_an_empty_frame_has_no_windows():
    assert split_windows(frame_prefix(0)) == []


@pytest.mark.parametrize("body", [
    b"", b"\x00\x00",                                              # short header
    frame_prefix(1) + window_prefix(10) + b"short",                # a length past the end
    frame_prefix(1) + window_prefix(1) + b"a" + b"junk",           # trailing bytes
    frame_prefix(0) + b"x",                                        # trailing bytes after an empty frame
    frame_prefix(2) + window_prefix(1) + b"a",                     # count says more than there is
    frame_prefix(3) + window_prefix(1) + b"a" + b"\x00\x00",       # a short window header
    frame_prefix(0xFFFFFFFF),                                      # a huge count with no content
])
def test_every_bad_frame_is_refused(body):
    with pytest.raises(BadFrame):
        split_windows(body)


def test_count_smaller_than_the_content_is_trailing_bytes():
    with pytest.raises(BadFrame):
        split_windows(frame_prefix(1) + window_prefix(1) + b"a" + window_prefix(1) + b"b")
