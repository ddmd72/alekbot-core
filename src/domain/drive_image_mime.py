"""
The vision input facts of the drive reader and triage (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §5.3, §24.5). Pure.

Only the four image formats every vision provider of the reader and triage takes are listed; a rendition's format is
read from its first bytes by `document_kind.image_mime` (Graph serves thumbnails and custom renditions as JPEG or PNG
whatever the original format, spike S0f, so the record's mime type does not describe them).
"""
from __future__ import annotations

VISION_MIME_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
# The largest image (raw bytes) the reader or triage sends: inline images travel base64-encoded, and 3.75 MiB encodes
# to exactly 5 MiB, Anthropic's per-image limit as its errors state it ("exceeds 5 MB maximum: N bytes > 5242880
# bytes", measured on the base64 data). OpenAI's limit is higher. The owner's live model check confirms the rule
# (scripts/validation/drive_model_check.py: an image at the cap, read by each provider).
MAX_VISION_IMAGE_BYTES = 3 * 1024 * 1024 + 768 * 1024
