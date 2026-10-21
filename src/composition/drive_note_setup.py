"""
STUB (plan part B's worktree only): part A owns this module (`src/composition/drive_note_setup.py`, plan part A); at
assembly A's file replaces this one. The two names below are verbatim from A's module, the only ones part B imports.
"""
from __future__ import annotations

from typing import Optional

DRIVE_NOTES_ENABLED_ENV = "DRIVE_NOTES_ENABLED"
_OFF = frozenset({"0", "off", "false", "no"})


def drive_notes_switch_on(raw: Optional[str]) -> bool:
    """The kill switch's value: on unless explicitly off (unset and empty are on)."""
    return str(raw or "").strip().lower() not in _OFF


__all__ = ["DRIVE_NOTES_ENABLED_ENV", "drive_notes_switch_on"]
