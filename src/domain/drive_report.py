"""The specialist's own report (DRIVE_SPECIALIST_RFC §7.1): the terminal tool's arguments, or a plain-text final reply."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class DriveReport:
    """The terminal tool's arguments (or a plain-text final reply, taken as the note)."""
    note: str = ""
    question: str = ""
    not_done: str = ""

    @classmethod
    def of(cls, args: Optional[Mapping[str, Any]], plain_text: str = "") -> "DriveReport":
        args = args or {}

        def text(name: str) -> str:
            value = args.get(name)
            return value.strip() if isinstance(value, str) else ""
        return cls(note=text("note") or plain_text.strip(), question=text("question"), not_done=text("not_done"))
