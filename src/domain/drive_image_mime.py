"""
The image type of a provider rendition, from its first bytes (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.5).
Pure.

Graph serves thumbnails and custom renditions as JPEG or PNG whatever the original format (HEIC as JPEG, SVG as PNG,
spike S0f), so the record's mime type does not describe them. Only the four formats every vision provider of the
reader and triage takes are named; anything else is "".
"""
from __future__ import annotations

VISION_MIME_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def image_mime(head: bytes) -> str:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return ""
