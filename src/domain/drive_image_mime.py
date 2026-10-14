"""
The image type of a provider rendition, from its first bytes (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.5).
Pure.

Graph serves thumbnails and custom renditions as JPEG or PNG whatever the original format (HEIC as JPEG, SVG as PNG,
spike S0f), so the record's mime type does not describe them. Only the four formats every vision provider of the
reader and triage takes are named; anything else is "".
"""
from __future__ import annotations

VISION_MIME_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
# The largest image (raw bytes) the reader or triage sends: inline images travel base64-encoded, and 3.75 MiB encodes
# to exactly 5 MiB, Anthropic's per-image limit as its errors state it ("exceeds 5 MB maximum: N bytes > 5242880
# bytes", measured on the base64 data). OpenAI's limit is higher. The owner's live model check confirms the rule
# (scripts/validation/drive_model_check.py: an image at the cap, read by each provider).
MAX_VISION_IMAGE_BYTES = 3 * 1024 * 1024 + 768 * 1024


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
