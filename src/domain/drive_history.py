"""The drive's history lines (DRIVE_SPECIALIST_RFC §7.4): what a turn's drive work did with which file. Pure.

A drive commission returns `history_context["drive_context"]`: one entry per item an operation touched, in run order,
no dedup. `history_entries` builds a collected result's entries: `{path, ref}` plus the operation (`op`) and its
detail (a search's `query` and `count`, a passages call's display numbers). ConversationHandler renders the turn's
entries with `drive_history_lines` into the model message's full_text only: one line per file, deduplicated, naming
what was done, keeping the ref so "open it" resolves on the next turn:

    [Drive: Health/lab-report.pdf ref=drive:abc123 — searched for "lab report" (2 files); read passages 3–5, 8]

The value may be a list of commission lists (the engine's fold) or a flat list of `{path, ref}` entries (older shape,
FileManagement's open and delete today); an entry without `op` renders as the file and its ref alone.

Public interface:
    DRIVE_CONTEXT_KEY
    history_entries(kind, outcome) -> [entry]
    drive_history_lines(value) -> [line]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional

from . import drive_answer_texts as t
from .drive_excerpt_chunk import display_number
from .drive_op import DriveOp
from .drive_outcome import DriveOutcome

DRIVE_CONTEXT_KEY = "drive_context"

OP_SEARCHED = "searched"
OP_SEARCHED_DELETED = "searched_deleted"
OP_LISTED = "listed"
OP_OPENED = "opened"
OP_OPEN_NO_CONTENT = "open_no_content"
OP_LINK = "link"
OP_PASSAGES = "passages"
OP_CHANGED = "changed"

_CHANGE_VERBS = {
    DriveOp.COPY.value: "copied in", DriveOp.SAVE_TEXT.value: "saved", DriveOp.CREATE_FOLDER.value: "created",
    DriveOp.MOVE.value: "moved or renamed", DriveOp.APPEND.value: "appended to",
    DriveOp.REPLACE.value: "content replaced",
}   # a delete touches no item that stays, so it has no entry
_PHRASES = {
    OP_SEARCHED_DELETED: "searched deleted recordings and notes",
    OP_LISTED: "listed",
    OP_OPENED: "opened whole",
    OP_OPEN_NO_CONTENT: "open answered without its content",
    OP_LINK: "recording link given",
}
_QUERY_MAX_CHARS = 100
_NESTING_MAX = 4   # the fold nests one level; deeper is not a shape any writer produces


def history_entries(kind: str, outcome: DriveOutcome) -> List[Dict[str, Any]]:
    """One collected result's `drive_context` entries: today's `{path, ref}` per item plus what was done."""
    detail: Dict[str, Any] = {"op": _op(kind, outcome)}
    if detail["op"] in (OP_SEARCHED, OP_SEARCHED_DELETED):
        detail["count"] = len(outcome.items)
        if outcome.request_text:
            detail["query"] = outcome.request_text
    elif detail["op"] == OP_CHANGED:
        detail["change"] = outcome.op.value
    numbers: Dict[str, List[int]] = {}
    if outcome.excerpts is not None:
        for f in outcome.excerpts.files:
            numbers.setdefault(f.ref, []).extend(display_number(c.index) for c in f.chunks)
    entries: List[Dict[str, Any]] = []
    for item in outcome.drive_context():
        entry: Dict[str, Any] = {**item, **detail}
        if detail["op"] == OP_PASSAGES:
            entry["passages"] = sorted(set(numbers.get(entry["ref"], [])))
        entries.append(entry)
    return entries


def drive_history_lines(value: Any) -> List[str]:
    """One line per file, in first-seen order; a file's operations combined in one line, each phrase once."""
    files: Dict[str, _File] = {}
    for entry in _entries(value, 0):
        ref = _clean(entry.get("ref"))
        path = _clean(entry.get("path"))
        if not ref and not path:
            continue
        line = files.setdefault(ref or path, _File(ref=ref))
        if path:
            line.path = path   # the latest path wins: a move renames the file within the turn
        line.add(entry)
    return [f.render() for f in files.values()]


def _op(kind: str, outcome: DriveOutcome) -> str:
    if outcome.is_change:
        return OP_CHANGED
    if outcome.op is DriveOp.PASSAGES:
        return OP_PASSAGES
    if outcome.op is DriveOp.LIST:
        return OP_LISTED
    if outcome.op is DriveOp.SEARCH:
        return OP_SEARCHED_DELETED if kind == t.KIND_DELETED_SEARCH else OP_SEARCHED
    if kind == t.KIND_LINK:
        return OP_LINK
    if outcome.op is DriveOp.OPEN:
        return OP_OPENED if (outcome.body or outcome.file_data) else OP_OPEN_NO_CONTENT
    return str(outcome.op.value)


def _entries(value: Any, depth: int) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
    elif isinstance(value, (list, tuple)) and depth < _NESTING_MAX:
        for item in value:
            yield from _entries(item, depth + 1)


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _count(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ranges(numbers: List[int]) -> str:
    """3, 4, 5, 8 -> "3–5, 8"."""
    runs: List[List[int]] = []
    for n in sorted(set(numbers)):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"{a}–{b}" if a != b else str(a) for a, b in runs)


@dataclass
class _File:
    ref: str
    path: str = ""
    phrases: List[str] = field(default_factory=list)
    passages: Optional[List[int]] = None   # None until a passages call touched the file

    def add(self, entry: Mapping[str, Any]) -> None:
        op = _clean(entry.get("op"))
        if not op:
            return
        if op == OP_PASSAGES:
            if self.passages is None:
                self.passages = []
                self._phrase(OP_PASSAGES)   # a placeholder: the numbers are joined at render
            raw = entry.get("passages")
            self.passages.extend(n for n in (_count(x) for x in (raw if isinstance(raw, (list, tuple)) else ()))
                                 if n is not None)
        elif op in (OP_SEARCHED, OP_SEARCHED_DELETED):
            self._phrase(_search_phrase(op, entry))
        elif op == OP_CHANGED:
            change = _clean(entry.get("change"))
            self._phrase(f"changed: {_CHANGE_VERBS.get(change, change)}" if change else "changed")
        else:
            self._phrase(_PHRASES.get(op, op))

    def _phrase(self, phrase: str) -> None:
        if phrase not in self.phrases:
            self.phrases.append(phrase)

    def render(self) -> str:
        done = "; ".join(self._passages_phrase() if p == OP_PASSAGES else p for p in self.phrases)
        head = f"[Drive: {self.path or '/'} ref={self.ref}" if self.ref else f"[Drive: {self.path}"
        return f"{head} — {done}]" if done else f"{head}]"

    def _passages_phrase(self) -> str:
        numbers = sorted(set(self.passages or []))
        if not numbers:
            return "read passages"
        return f"read passage {numbers[0]}" if len(numbers) == 1 else f"read passages {_ranges(numbers)}"


def _search_phrase(op: str, entry: Mapping[str, Any]) -> str:
    base = _PHRASES[op] if op == OP_SEARCHED_DELETED else "searched"
    query = _clean(entry.get("query")).replace('"', "'")
    if query:
        query = query if len(query) <= _QUERY_MAX_CHARS else query[: _QUERY_MAX_CHARS - 1] + "…"
        base = f'{base} for "{query}"'
    count = _count(entry.get("count"))
    if count is not None:
        base = f"{base} ({count} file{'' if count == 1 else 's'})"
    return base
