"""YAML front matter that a YAML reader parses back to exactly the values written. Pure, stdlib only (the domain takes
no YAML library).

Why one form for every value: a value here is text a model wrote (a task's title, its note), so it may hold anything,
a `---` line, a colon, quotes or line breaks included. A YAML double-quoted scalar is the one form in which any text is
a single line that a reader turns back into the same string, so no value can end the front matter early or add a key.
Escaped: the backslash, the double quote, every line break (`\\n`, `\\r`, NEL, LINE and PARAGRAPH SEPARATOR: a reader
folds a raw one into a space), the tab, the byte order mark, and every character outside YAML's printable set (C0 and
C1 controls, DEL, surrogates, U+FFFE, U+FFFF). Everything else stays as it is, so the owner reads a note in their own
script, not as escapes.

Keys are written as given: callers pass plain lowercase words.

Public interface:
    front_matter(fields) -> str      `---`, one `key: "value"` line per field, `---`, each line ending in a newline
"""
from __future__ import annotations

from typing import Sequence, Tuple

_SHORT = {"\\": "\\\\", "\"": "\\\"", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_LINE_BREAKS = frozenset({"\x85", " ", " ", "﻿"})


def _printable(ch: str) -> bool:
    """YAML 1.1's printable set, as PyYAML's reader checks it, less the line breaks a double-quoted scalar folds."""
    code = ord(ch)
    return ((0x20 <= code <= 0x7E or 0xA0 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or code >= 0x10000)
            and ch not in _LINE_BREAKS)


def _escaped(ch: str) -> str:
    if ch in _SHORT:
        return _SHORT[ch]
    if _printable(ch):
        return ch
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02X}"
    return f"\\u{code:04X}"     # every non-printable character above U+00FF is in the BMP


def quoted(value: str) -> str:
    """`value` as a YAML double-quoted scalar on one line."""
    return "\"" + "".join(_escaped(ch) for ch in value) + "\""


def front_matter(fields: Sequence[Tuple[str, str]]) -> str:
    """The front matter block: an opening `---` line, one `key: "value"` line per field in order, a closing `---`."""
    lines = ["---", *(f"{key}: {quoted(value)}" for key, value in fields), "---"]
    return "".join(f"{line}\n" for line in lines)


__all__ = ["front_matter", "quoted"]
