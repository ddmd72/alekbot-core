"""One text the commission asks to write (DRIVE_SPECIALIST_RFC §4.2): an item of `file_contents`, bound by code."""
from __future__ import annotations

from dataclasses import dataclass

FIRST_LINE_MAX_CHARS = 80


@dataclass(frozen=True)
class FileContentItem:
    handle: str
    text: str
    name: str = ""
    target: str = ""

    def describe(self) -> str:
        """`t1 name "notes.md" 120 characters, first line "…"` (§5.3)."""
        lines = self.text.splitlines()
        first = lines[0] if lines else ""
        if len(first) > FIRST_LINE_MAX_CHARS:
            first = first[:FIRST_LINE_MAX_CHARS] + "…"
        who = f'name "{self.name}" ' if self.name else (f"target {self.target} " if self.target else "")
        return f'{self.handle} {who}{len(self.text)} characters, first line "{first}"'
