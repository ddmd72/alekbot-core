"""
"Where it comes from": the folder notes and file lines shown with passages
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §28.3 A18). Pure.

Folder notes print once per run and never become the file's (display, not inheritance: DRIVE_FILE_NOTES_RFC D8,
D20). The cap counts folder lines only (OQ-A3): a file's head, own note and description are never dropped. Folder
lines are dropped whole, farthest first: by the fewest levels from the folder down to a shown file under it, then
nearer the root first, then by path.

Public interface:
    ExcerptContext(files, folders)      the context one result carries; EMPTY; merge(other)
    quoted(text) -> str                 a name or path in a code line: double-quoted, `"` `«` `»` escaped
    file_description(rec) -> str        the file's what-it-is line (model-written: shown inside the wrapper)
    folder_line(note) -> str            `folder "<path>": noted by the owner: «…»`, escaped for outside a wrapper
    plan_context(files, folders, *, shown_folders, described_files, chars_max) -> ContextPlan
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, FrozenSet, List, Mapping, Sequence, Tuple

from .drive_enrichment import ProfileSource
from .drive_excerpt_context_plan import ContextPlan
from .drive_excerpt_file_context import FileContext
from .drive_excerpt_folder_note import FolderNote
from .drive_index import DriveIndexRecord
from .drive_index_params import EXCERPT_DESCRIPTION_CHARS
from .drive_profile import TemplateReason, template_profile
from .drive_search import escape_text

# The one template what-it-is that is not a copy of the file's own text (drive_profile.template_profile).
_MACHINE_TEXT_LABEL = template_profile(TemplateReason.MACHINE_TEXT, "", "").what_it_is
_ELLIPSIS = "…"


def quoted(text: str) -> str:
    """Escaping `«` `»` keeps an owner line (`noted by the owner: «…»`) recognisable by its shape."""
    inner = escape_text(text).replace("\\", "\\\\").replace('"', '\\"').replace("«", "\\«").replace("»", "\\»")
    return f'"{inner}"'


@dataclass(frozen=True)
class ExcerptContext:
    """`files`: one per result file, in result order; `folders`: the noted ancestor folders, by folder item id."""
    EMPTY: ClassVar["ExcerptContext"]

    files: Tuple[FileContext, ...] = ()
    folders: Mapping[str, FolderNote] = field(default_factory=dict)

    def merge(self, other: "ExcerptContext") -> "ExcerptContext":
        """Self first: a file or folder already held keeps its first context."""
        known = {f.item_id for f in self.files}
        files: List[FileContext] = list(self.files)
        for f in other.files:
            if f.item_id not in known:
                known.add(f.item_id)
                files.append(f)
        folders: Dict[str, FolderNote] = dict(self.folders)
        for item_id, note in other.folders.items():
            folders.setdefault(item_id, note)
        return ExcerptContext(files=tuple(files), folders=folders)


ExcerptContext.EMPTY = ExcerptContext()


def _cut(text: str, limit: int = EXCERPT_DESCRIPTION_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit - len(_ELLIPSIS) + 1]
    space = head.rfind(" ")
    head = head[:space] if space > 0 else head[:-1]
    return head.rstrip() + _ELLIPSIS


def file_description(rec: DriveIndexRecord) -> str:
    """The profile's `what_it_is` when it describes the readable version, except a template copy of a short note's
    own text (it would repeat the passage); else the reader's brief; else ""."""
    profile = rec.profile
    if profile is not None and rec.indexed_version is not None and rec.profiled_content_version == rec.indexed_version:
        text = profile.what_it_is
        copy = rec.profile_source == ProfileSource.TEMPLATE.value and text != _MACHINE_TEXT_LABEL
        if text.strip() and not copy:
            return _cut(text)
    if rec.brief is not None and rec.brief.description.strip():
        return _cut(rec.brief.description)
    return ""


def folder_line(note: FolderNote) -> str:
    return f"folder {quoted(note.path)}: {escape_text(note.line)}"


def _distances(files: Sequence[FileContext], folders: Mapping[str, FolderNote],
               shown_folders: FrozenSet[str]) -> Dict[str, int]:
    """Candidate folder id -> fewest levels down to a shown file under it (the nearest ancestor is 1)."""
    dist: Dict[str, int] = {}
    for f in files:
        for level, folder_id in enumerate(f.ancestors, start=1):
            note = folders.get(folder_id)
            if note is None or not note.line or folder_id in shown_folders:
                continue
            dist[folder_id] = min(dist.get(folder_id, level), level)
    return dist


def plan_context(files: Sequence[FileContext], folders: Mapping[str, FolderNote], *, shown_folders: FrozenSet[str],
                 described_files: FrozenSet[str], chars_max: int) -> ContextPlan:
    """`files` in render order. Each kept folder prints right before the first file under it, root first."""
    unique: List[FileContext] = []
    for f in files:
        if all(u.item_id != f.item_id for u in unique):
            unique.append(f)
    dist = _distances(unique, folders, shown_folders)
    kept = set(dist)
    total = sum(len(folder_line(folders[i])) for i in kept)
    dropped: List[str] = []
    for folder_id in sorted(dist, key=lambda i: (-dist[i], folders[i].depth, folders[i].path)):
        if total <= chars_max:
            break
        kept.discard(folder_id)
        total -= len(folder_line(folders[folder_id]))
        dropped.append(folders[folder_id].path)
    before: Dict[str, Tuple[FolderNote, ...]] = {}
    placed: set[str] = set()
    for f in unique:
        mine = [folders[i] for i in f.ancestors if i in kept and i not in placed]
        placed.update(n.item_id for n in mine)
        if mine:
            before[f.item_id] = tuple(sorted(mine, key=lambda n: (n.depth, n.path)))
    described = frozenset(f.item_id for f in unique if f.item_id not in described_files)
    owner_note = any(f.own_note for f in unique if f.item_id in described)
    return ContextPlan(before=before, described=described, dropped=tuple(dropped),
                       shown_after=frozenset(shown_folders | kept), has_owner_line=bool(kept) or owner_note)
