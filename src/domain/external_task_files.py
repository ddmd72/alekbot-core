"""The names, storage keys and shown sizes of a task's files (EXTERNAL_TASK_QUEUE_RFC §5.12). Pure, stdlib only.

A file's name comes from wherever the owner's file lives (a drive item, an email attachment, a chat upload), so it may
hold anything: a path, line breaks, control or bidirectional characters, a name a storage key cannot carry. The stored
name is that name made safe once, at create: the last path segment, Unicode NFC, every control, format, separator
(other than the space) and storage-reserved character replaced by `_`, leading dots dropped, and cut to
`TASK_FILE_NAME_MAX_CHARS` with the extension kept. The same name is the key's last segment, the receipt's, the claim's
and the link answer's, so the owner confirms exactly the name the worker gets.

Public interface:
    task_file_name(raw) -> str                                   the stored name; never empty
    task_file_key(user_id, task_id, number, name) -> str         `external_tasks/{user_id}/{task_id}/{number}-{name}`
    size_label(size) -> str                                      bytes as the owner reads them (B, KB, MB)
"""
from __future__ import annotations

import unicodedata

from .external_task import TASK_FILE_NAME_MAX_CHARS

TASK_FILES_PREFIX = "external_tasks"
_FALLBACK_NAME = "file"
_RESERVED = frozenset('/\\#?[]*:"<>|')
_MAX_EXTENSION_CHARS = 16


def _safe(ch: str) -> str:
    if ch == " ":
        return ch
    if ch in _RESERVED or unicodedata.category(ch)[0] in ("C", "Z"):
        return "_"
    return ch


def task_file_name(raw: str) -> str:
    """The name a task file is stored and shown under: never empty, never a path, at most `TASK_FILE_NAME_MAX_CHARS`."""
    last = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    name = "".join(_safe(ch) for ch in unicodedata.normalize("NFC", last)).strip().lstrip(".").strip()
    if not name:
        return _FALLBACK_NAME
    if len(name) <= TASK_FILE_NAME_MAX_CHARS:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext) <= _MAX_EXTENSION_CHARS:
        return stem[:TASK_FILE_NAME_MAX_CHARS - len(ext) - 1].rstrip() + "." + ext
    return name[:TASK_FILE_NAME_MAX_CHARS].rstrip()


def task_file_key(user_id: str, task_id: str, number: int, name: str) -> str:
    """The media bucket key of one copy; the bucket's lifecycle rule for the prefix deletes it by age."""
    return f"{TASK_FILES_PREFIX}/{user_id}/{task_id}/{number}-{name}"


def size_label(size: int) -> str:
    """`980 B`, `24.0 KB`, `1.5 MB`: binary units, one decimal above bytes."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


__all__ = ["TASK_FILES_PREFIX", "size_label", "task_file_key", "task_file_name"]
