"""One entry of a drive connection's name history (MULTI_DRIVE_RFC §11.8), and the one comparison key for mount names.

`mount_name_key` lives here, the leaf of the mount modules, so `drive_connection` can check its history without importing
`drive_mounts` (which imports the connection); `drive_mounts` re-exports it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class MountNameChange:
    """The connection took `name` at `at` (tz-aware UTC)."""
    name: str
    at: datetime


def normalise_mount_name(name: str) -> str:
    """NFC, trimmed, inner runs of whitespace collapsed to one space: the form a mount name is stored and shown in."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", name).strip())


def mount_name_key(name: str) -> str:
    """The comparison key for mount names: the normalised name, case-folded."""
    return normalise_mount_name(name).casefold()
