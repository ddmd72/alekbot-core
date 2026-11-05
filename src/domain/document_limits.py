"""
The caps table of external files (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.5): every byte and character cap of opening a
file, in one place, by store and kind. Pure, stdlib only: the parser child imports `drive_index_params`, which imports
`OPEN_MAX_CHARS` from here, so nothing heavier may be imported.

A store is a `file_ref.FileRefKind` value (drive, chat, email, workspace, delivered). Skill files are never opened
through this table (§4.1), so `skill`, like an unknown store, is refused.

Caps that stay where they are (a provider's fact or another store's own rule), cited here so the table is complete:
    GOOGLE_EXPORT_MAX_BYTES          10 MiB  src/adapters/google/google_drive_mapping.py (Google's export cap)
    MAX_VISION_IMAGE_BYTES           3.75 MiB src/domain/drive_image_mime.py (the reader's vision input, base64-safe)
    MAX_DRIVE_APPEND_FILE_BYTES,
    MAX_DRIVE_TEXT_FILE_BYTES        5 MiB   src/domain/user_drive.py (what the bot writes, not what it opens)
    workspace.MAX_FILE_BYTES         10 MiB  src/domain/workspace.py (the workspace store's own cap: a different
                                             constant with the same name as the one below; never merged)

Public interface:
    MAX_FILE_BYTES, MAX_CONVERTED_CHARS, MAX_TEXT_EXPORT_BYTES, MAX_DRIVE_DOWNLOAD_BYTES,
    MAX_DRIVE_VISION_IMAGE_BYTES, MAX_DRIVE_VISION_PDF_BYTES, OPEN_MAX_CHARS, MAX_VIDEO_ATTACH_BYTES
    parse_input_cap(store) -> int;  text_export_cap() -> int;  native_ceiling(kind) -> int
    presentation_cap(store) -> int;  reader_text_budget(store) -> int;  action_cap() -> int
"""
from __future__ import annotations

from typing import FrozenSet

MAX_FILE_BYTES = 5 * 1024 * 1024    # 5 MB — input to text parsing; the context window would explode beyond this
MAX_CONVERTED_CHARS = 60_000         # D20: the one presentation cut of parser text outside the drive (chat, email, …)
# The largest text a drive may hand over for one file: a document export's cap (GOOGLE_DRIVE_RFC G1b). The 5 MiB
# input cap applies to the text after its inline `data:` images are dropped.
MAX_TEXT_EXPORT_BYTES = 10 * 1024 * 1024
# Drive size caps, all checked on metadata before any download (USER_DRIVE_RFC §4.6, §4.9).
MAX_DRIVE_DOWNLOAD_BYTES = 45 * 1024 * 1024       # = FileManagementAgent's video re-send cap
MAX_DRIVE_VISION_IMAGE_BYTES = 5 * 1024 * 1024    # below the strictest provider image limit
MAX_DRIVE_VISION_PDF_BYTES = 20 * 1024 * 1024     # below the strictest provider PDF/inline limit
# USER_DRIVE_INDEXING_RFC §29: the most of a drive text an open returns (about 30 printed pages); shared with
# transcripts (`drive_index_params.TRANSCRIPT_OPEN_MAX_CHARS` is an alias).
OPEN_MAX_CHARS = 60_000
# The bytes an action (save, send) may carry. Safety margin under Telegram's 50MB bot-API document cap (Slack's own
# limit is far higher). video_generation/ files are duration-capped (VIDEO_GENERATION_RFC.md §3.11 decision #10, 10s
# hard cap), so a real file almost never approaches this; it only makes an anomalous file degrade to a message
# instead of a failed upload.
MAX_VIDEO_ATTACH_BYTES = 45 * 1024 * 1024

_DRIVE = "drive"
_EMAIL = "email"
_STORES: FrozenSet[str] = frozenset({_DRIVE, "chat", _EMAIL, "workspace", "delivered"})


def _check(store: str) -> None:
    if store not in _STORES:
        raise ValueError(f"no caps for store {store!r}")


def parse_input_cap(store: str) -> int:
    """Bytes a file may have to be parsed as text, the same in every store (email's own 3 MB stays in its service
    until U4)."""
    _check(store)
    return MAX_FILE_BYTES


def text_export_cap() -> int:
    """Bytes a plain text or a text export may have before its inline `data:` images are dropped."""
    return MAX_TEXT_EXPORT_BYTES


def native_ceiling(kind: str) -> int:
    """The most a consumer's native declaration can allow for a kind (§4.3): 5 MiB an image, 20 MiB a PDF; any other
    kind is never sent natively (0)."""
    if kind == "image":
        return MAX_DRIVE_VISION_IMAGE_BYTES
    if kind == "pdf":
        return MAX_DRIVE_VISION_PDF_BYTES
    return 0


def presentation_cap(store: str) -> int:
    """Characters of text a store presents (D20: one cut on every open path): the drive's §29 open bound, 60 000
    (`MAX_CONVERTED_CHARS`) for every other store, email included."""
    _check(store)
    if store == _DRIVE:
        return OPEN_MAX_CHARS
    return MAX_CONVERTED_CHARS


def reader_text_budget(store: str) -> int:
    """Characters the document reader may return for one file (D7): the store's presentation cap, so a scan is read
    only as far as it can be shown (A4/D17 for the drive, D20 elsewhere)."""
    return presentation_cap(store)


def action_cap() -> int:
    """Bytes a save or send may carry."""
    return MAX_VIDEO_ATTACH_BYTES
