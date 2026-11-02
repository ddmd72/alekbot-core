"""The answer of `drive_mounts.validate_mount_name` (MULTI_DRIVE_RFC §11.8)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MountNameCheck:
    name: str          # the normalised name (NFC, trimmed, inner runs of whitespace collapsed)
    error: str = ""    # "" = valid; else one Cabinet message
    warning: str = ""  # the shadowing warning, only when valid

    @property
    def ok(self) -> bool:
        return not self.error
