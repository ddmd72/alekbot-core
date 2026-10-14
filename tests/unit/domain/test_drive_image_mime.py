"""Rendition type from magic bytes (USER_DRIVE_INDEXING_RFC §5.3, §24.5). Pure."""
import pytest

from src.domain.drive_image_mime import VISION_MIME_TYPES, image_mime


@pytest.mark.parametrize("head,mime", [
    (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n\x00\x00", "image/png"),
    (b"GIF89a\x01\x00", "image/gif"),
    (b"GIF87a\x01\x00", "image/gif"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
])
def test_vision_formats(head, mime):
    assert image_mime(head) == mime and mime in VISION_MIME_TYPES


@pytest.mark.parametrize("head", [b"", b"\x00\x00\x00\x18ftypheic", b"<svg", b"%PDF-1.4", b"RIFF\x00\x00\x00\x00WAVE",
                                  b"RIFF"])
def test_anything_else_is_unknown(head):
    assert image_mime(head) == ""
