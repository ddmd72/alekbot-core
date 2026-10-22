"""The commission's one field, `file_contents` (DRIVE_SPECIALIST_RFC §4.2): text to write, bound by code.

Each item gets a handle (`t1`, `t2`, …) shown in the state block with its name or target, length and first line; a
write tool names the handle and code writes that item's text, so the specialist's model never retypes content.
Accept liberally: a list of items, one item, or a bare string (one unnamed item); `text` or `append_text`; `target`
or `file_ref` as the target. Anything else is skipped, never rejected.

Waiver (one public class per file): `FileContentItem` is the value type `parse_file_contents` returns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Tuple

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


def parse_file_contents(payload: Mapping[str, Any]) -> Tuple[FileContentItem, ...]:
    raw = payload.get("file_contents")
    if raw is None and isinstance(payload.get("text"), str):   # a stray Smart-era save shape
        raw = [{"text": payload["text"], "name": payload.get("name") or ""}]
    if raw is None and isinstance(payload.get("append_text"), str):
        raw = [{"text": payload["append_text"], "target": payload.get("file_ref") or ""}]
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return ()
    items: List[FileContentItem] = []
    for entry in raw:
        if isinstance(entry, str):
            entry = {"text": entry}
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            text = entry.get("append_text")
        if not isinstance(text, str):
            continue
        name = entry.get("name")
        target = entry.get("target") or entry.get("file_ref")
        items.append(FileContentItem(handle=f"t{len(items) + 1}", text=text,
                                     name=name if isinstance(name, str) else "",
                                     target=target if isinstance(target, str) else ""))
    return tuple(items)
