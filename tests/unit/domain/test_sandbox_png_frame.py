"""render.png body frame: u32-be width | html."""
from __future__ import annotations

import pytest

from src.domain.sandbox_png_frame import BadPngBody, frame_png_body, split_png_body

_HTML = "<h1>Albergo Immaginario</h1><p>RM-55120, 10–14 Aug 2025, 560 EUR</p>".encode()


def test_round_trip() -> None:
    assert split_png_body(frame_png_body(800, _HTML)) == (800, _HTML)


def test_bounds_round_trip() -> None:
    assert split_png_body(frame_png_body(16, _HTML))[0] == 16
    assert split_png_body(frame_png_body(2048, _HTML))[0] == 2048


@pytest.mark.parametrize("width", [15, 2049, True])
def test_bad_width_refused(width: int) -> None:
    with pytest.raises(BadPngBody):
        frame_png_body(width, _HTML)


def test_short_body_refused() -> None:
    with pytest.raises(BadPngBody):
        split_png_body(b"\x00\x00\x03")


def test_empty_html_round_trips() -> None:
    assert split_png_body(frame_png_body(640, b"")) == (640, b"")
