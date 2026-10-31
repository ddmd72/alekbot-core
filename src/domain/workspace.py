"""Agent workspace — Smart's own persistent working files (docs/10_rfcs/AGENT_WORKSPACE_RFC.md).

Pure: the path rule, kinds, caps, reminder-field values, line windows, exact-string replacement and every text the
`workspace` tool shows. No I/O: WorkspaceService (services/) does the work through WorkspaceStorePort.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel

from .drive_ref import REF_PREFIX as DRIVE_REF_PREFIX
from .exceptions import WorkspaceRefused
from .skill import MAX_SKILL_FILE_BYTES, SKILL_FILE_EXTENSIONS

WORKSPACE_TOOL = "workspace"
WORKSPACE_REF_PREFIX = "workspace:"
COMMANDS: Tuple[str, ...] = ("view", "create", "str_replace", "delete", "rename", "copy", "history", "restore")
MUTATING_COMMANDS = frozenset({"create", "str_replace", "delete", "rename", "copy", "restore"})

FOLDERS: Tuple[str, ...] = ("projects/", "references/", "scratch/", "files/")
TEXT_FOLDERS: Tuple[str, ...] = ("projects/", "references/", "scratch/")
COPY_FOLDER = "files/"
SCRATCH_FOLDER = "scratch/"
TEXT_EXTENSIONS: Tuple[str, ...] = SKILL_FILE_EXTENSIONS

# Caps (D4, §6). Soft: two concurrent creates can land one file over.
MAX_FILES_PER_USER = 100
MAX_BYTES_PER_USER = 100 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_FILE_BYTES = MAX_SKILL_FILE_BYTES
MAX_PATH_CHARS = 120
MAX_PATH_SEGMENTS = 4
VIEW_DEFAULT_LINES = 2000
VIEW_MAX_LINE_CHARS = 2000
VIEW_MAX_BYTES = 100 * 1024
LISTING_MAX_ENTRIES = 100

# The reminder field workspace_file (§8.3, D15): "remove" clears (the draft_skill vocabulary); fillers a model writes
# into unused fields mean "not provided", so the field is never cleared by accident.
REMOVE_VALUE = "remove"
_NOT_PROVIDED = frozenset({"", "none", "null", "n/a"})

# In the `instruction` parameter description of both NotesAgent tools (§8.3).
BINDING_INSTRUCTION_NOTE = (
    "When a workspace file is attached to the reminder, keep this short and let it refer to the file."
)
BINDING_ONE_REMINDER = "workspace_file applies to one reminder per request; create or update exactly one"
BINDING_NOT_APPLIED = "workspace_file was not applied"

_ALLOWED = re.compile(r"^[a-z0-9/._-]+$")
_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FILE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*\.[a-z0-9]+$")
_FOLDER_NAMES = frozenset(f.rstrip("/") for f in FOLDERS)
_GENERATION_HINT = "generation must be a version number from history or a view header"


def _invalid(raw: str, rule: str) -> WorkspaceRefused:
    return WorkspaceRefused(f"invalid workspace path '{raw}': {rule}", reason="path")


def parse_workspace_path(raw: object, *, allow_folder: bool = False) -> str:
    """The path without its `workspace:` prefix (§6).

    With `allow_folder` (view, history) also "" for the whole workspace, or a folder prefix ending in "/"; a bare
    folder name or an extension-less last segment is read as a folder. Raises WorkspaceRefused naming the rule, or
    the right tool for a ref of another store (§7.3 "Wrong store").
    """
    text = str(raw if raw is not None else "").strip()
    if text.startswith(DRIVE_REF_PREFIX):   # the drive ref codec's prefix (MULTI_DRIVE_RFC §9), tagged or not
        raise WorkspaceRefused(
            f"'{text}' is on the user's drive, not in the workspace; open it through work_with_drive_files",
            reason="wrong_store",
        )
    if text.startswith("skill:"):
        raise WorkspaceRefused(
            f"'{text}' is a skill file, not a workspace file; open it with open_file", reason="wrong_store",
        )
    path = text[len(WORKSPACE_REF_PREFIX):] if text.startswith(WORKSPACE_REF_PREFIX) else text
    if path == "":
        if allow_folder:
            return ""
        raise _invalid(text, "a path is required, e.g. projects/<name>.md")
    if allow_folder and path.rstrip("/") in _FOLDER_NAMES and path.count("/") <= 1:
        return path.rstrip("/") + "/"
    if "/" not in path:
        raise WorkspaceRefused(
            f"'{text}' is not a workspace path (paths start with projects/, references/, scratch/ or files/); "
            f"to keep a chat file, use copy with source \"{text}\" and a destination path "
            "(a text file: any folder; a binary file: under files/)",
            reason="wrong_store",
        )
    if len(path) > MAX_PATH_CHARS:
        raise _invalid(text, f"at most {MAX_PATH_CHARS} characters")
    if not _ALLOWED.fullmatch(path):
        raise _invalid(text, "use lowercase letters, digits, '-', '_', '.' and '/' only")
    is_folder = path.endswith("/")
    body = path[:-1] if is_folder else path
    if body.startswith("/") or body.endswith("/") or "//" in body:
        raise _invalid(text, "no leading '/', no trailing '/', no '//'")
    segments = body.split("/")
    if len(segments) > MAX_PATH_SEGMENTS:
        raise _invalid(text, f"at most {MAX_PATH_SEGMENTS} segments")
    if any(not _SEGMENT.fullmatch(s) for s in segments):
        raise _invalid(text, "every segment starts with a letter or digit; '.' and '..' are not allowed")
    if segments[0] + "/" not in FOLDERS:
        raise _invalid(text, "start with projects/, references/, scratch/ or files/")
    if is_folder:
        if not allow_folder:
            raise _invalid(text, "a file path has no trailing '/'")
        return path
    if not _FILE_NAME.fullmatch(segments[-1]):
        if allow_folder:
            return path + "/"
        raise _invalid(text, "the file name needs an extension, e.g. .md")
    return path


def workspace_kind(path: str) -> str:
    """`text` for the skill-file extensions (§6), else `binary`. UTF-8 is checked where the bytes are read."""
    return "text" if path.lower().endswith(TEXT_EXTENSIONS) else "binary"


def folder_of(path: str) -> str:
    return path.split("/", 1)[0] + "/"


def is_text(path: str, data: bytes) -> bool:
    """A text-kind extension AND valid UTF-8 (§6)."""
    if workspace_kind(path) != "text":
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def fire_key(note_id: str, due_at: datetime) -> str:
    """One reminder fire: the same note and due time on every Cloud Tasks re-run of it (§10 row 1)."""
    return f"reminder:{note_id}@{due_at.isoformat()}"


def parse_generation(raw: object) -> Optional[int]:
    """A version number (§5.3 R6, passed as a string). Fillers and "0" mean "not given"."""
    if isinstance(raw, bool):
        raise WorkspaceRefused(_GENERATION_HINT, reason="generation")
    text = str(raw if raw is not None else "").strip()
    if text.lower() in _NOT_PROVIDED or text == "0":
        return None
    if not (text.isascii() and text.isdigit()):
        raise WorkspaceRefused(_GENERATION_HINT, reason="generation")
    return int(text)


@dataclass(frozen=True)
class ReminderField:
    """The `workspace_file` value from the delegation context (§8.3): a value, "remove", or not provided."""

    value: Optional[str] = None
    clear: bool = False

    @property
    def provided(self) -> bool:
        return self.clear or self.value is not None


def parse_reminder_field(raw: object) -> ReminderField:
    if raw is None:
        return ReminderField()
    text = str(raw).strip()
    lowered = text.lower()
    if lowered == REMOVE_VALUE:
        return ReminderField(clear=True)
    if lowered in _NOT_PROVIDED:
        return ReminderField()
    return ReminderField(value=text)


def validate_workspace_file(raw: str) -> str:
    """The normalised path of a reminder's workspace file: any valid file path, any folder (D17); it need not exist
    yet — a first run may create it (§8.3)."""
    try:
        return parse_workspace_path(raw)
    except WorkspaceRefused as e:
        raise WorkspaceRefused(f"workspace_file: {e}", reason="workspace_file") from e


def binding_system_line(field: ReminderField) -> str:
    """The line code appends to NotesAgent's user message when the field is provided (§8.3, B3)."""
    if field.clear:
        return (
            "System: the workspace file will be removed by code from the reminder you update in this request. "
            "Call update_self_reminder with its note_id even if nothing else changes."
        )
    return (
        f"System: workspace_file={field.value} will be attached by code to the reminder you create or update in "
        "this request. Call update_self_reminder with its note_id even if nothing else changes. Keep the "
        "instruction short and let it refer to the file."
    )


# ---------------------------------------------------------------------------------------------------------------------
# Stored objects (§5.2, §13)
# ---------------------------------------------------------------------------------------------------------------------


class WorkspaceEntry(BaseModel):
    """One version of one file. `updated` is when this version was written (GCS timeCreated); `deleted_at` is when it
    stopped being live (GCS timeDeleted, noncurrent versions only). The deletion keys are read only on noncurrent
    versions: a live object can carry them after a delete that failed after its metadata patch (§5.3)."""

    path: str
    generation: int
    size: int
    updated: datetime
    origin: str = ""
    fire: Optional[str] = None
    write_id: Optional[str] = None
    live: bool = True
    deleted_at: Optional[datetime] = None
    deleted_by: Optional[str] = None
    deleted_fire: Optional[str] = None
    delete_id: Optional[str] = None
    renamed_to: Optional[str] = None
    restored_from: Optional[int] = None


class WorkspaceBlob(BaseModel):
    data: bytes
    entry: WorkspaceEntry


class WorkspaceWriteMeta(BaseModel):
    """Stamped on every put (§5.2); the adapter adds a fresh `write_id`."""

    origin: str
    fire: Optional[str] = None
    restored_from: Optional[int] = None


class WorkspaceDeleteMeta(BaseModel):
    """Patched onto the live version just before its delete (§5.2, B4); the adapter adds a fresh `delete_id`."""

    deleted_by: str
    deleted_fire: Optional[str] = None
    renamed_to: Optional[str] = None


# ---------------------------------------------------------------------------------------------------------------------
# Caps (§6)
# ---------------------------------------------------------------------------------------------------------------------


def check_caps(live: Sequence[WorkspaceEntry], adding_bytes: int) -> None:
    """Called only by commands that add a live file (create of a new path, copy, restore of a deleted file)."""
    if len(live) >= MAX_FILES_PER_USER:
        raise WorkspaceRefused(f"workspace full ({MAX_FILES_PER_USER} files); delete or merge first", reason="caps")
    if sum(e.size for e in live) + adding_bytes > MAX_BYTES_PER_USER:
        raise WorkspaceRefused(
            f"workspace full ({MAX_BYTES_PER_USER // (1024 * 1024)} MB); delete or merge first", reason="caps",
        )


def check_replace_caps(live: Sequence[WorkspaceEntry], old_size: int, new_size: int) -> None:
    """Byte cap for an overwrite of a live file (the file count does not change)."""
    if sum(e.size for e in live) - old_size + new_size > MAX_BYTES_PER_USER:
        raise WorkspaceRefused(
            f"workspace full ({MAX_BYTES_PER_USER // (1024 * 1024)} MB); delete or merge first", reason="caps",
        )


def check_file_size(path: str, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceRefused(f"{path}: file over {MAX_FILE_BYTES // (1024 * 1024)} MB", reason="caps")
    if len(data) > MAX_TEXT_FILE_BYTES and is_text(path, data):
        raise WorkspaceRefused(f"{path}: text file over {MAX_TEXT_FILE_BYTES // 1024} KB", reason="caps")


# ---------------------------------------------------------------------------------------------------------------------
# view windows (§6) and str_replace (§7.3)
# ---------------------------------------------------------------------------------------------------------------------


def _positive_int(raw: object) -> Optional[int]:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    text = str(raw).strip()
    if text.isascii() and text.lstrip("-").isdigit():
        n = int(text)
        return n if n > 0 else None
    return None


def view_window(offset: object, limit: object) -> Tuple[int, int]:
    """(first line, line count). Empty, zero, negative or unreadable values mean the defaults (§6)."""
    return _positive_int(offset) or 1, _positive_int(limit) or VIEW_DEFAULT_LINES


@dataclass(frozen=True)
class TextWindow:
    text: str
    first: int
    last: int
    total: int

    @property
    def cut(self) -> bool:
        return self.last < self.total


def number_lines(text: str, offset: object = None, limit: object = None) -> TextWindow:
    """`cat -n` lines (6-wide number, tab), each cut at 2,000 chars, the whole window at most 100 KB (§6)."""
    lines = text.splitlines()
    first, count = view_window(offset, limit)
    out: List[str] = []
    used = 0
    for n in range(first, min(len(lines), first + count - 1) + 1):
        line = lines[n - 1]
        if len(line) > VIEW_MAX_LINE_CHARS:
            line = line[:VIEW_MAX_LINE_CHARS] + " …[line cut]"
        rendered = f"{n:6d}\t{line}"
        used += len(rendered.encode("utf-8")) + 1
        if out and used > VIEW_MAX_BYTES:
            break
        out.append(rendered)
    last = first + len(out) - 1 if out else min(first - 1, len(lines))
    return TextWindow(text="\n".join(out), first=first, last=last, total=len(lines))


def window_note(w: TextWindow) -> str:
    if w.total and w.first > w.total:
        return f"(the file has {w.total} lines; offset {w.first} is past the end)"
    if w.cut:
        return f"…lines {w.first}–{w.last} of {w.total}; view with offset={w.last + 1} to read on"
    return ""


@dataclass(frozen=True)
class Replacement:
    text: str
    first_line: int
    line_count: int


def apply_str_replace(path: str, text: str, old: str, new: str) -> Replacement:
    """Replace the one occurrence of `old` (§7.3). Refused when `old` is empty, absent or not unique."""
    if not old:
        raise WorkspaceRefused("old_str is empty; pass the exact text to replace", reason="no_match")
    count = text.count(old)
    if count == 0:
        raise WorkspaceRefused(f"old_str not found in {path}", reason="no_match")
    if count > 1:
        raise WorkspaceRefused(
            f"found {count} matches of old_str in {path}; include more surrounding text", reason="multi_match",
        )
    index = text.index(old)
    return Replacement(
        text=text[:index] + new + text[index + len(old):],
        first_line=text.count("\n", 0, index) + 1,
        line_count=max(1, new.count("\n") + 1),
    )


def changed_lines(r: Replacement) -> str:
    return number_lines(r.text, r.first_line, r.line_count).text


# ---------------------------------------------------------------------------------------------------------------------
# What view and history show (§7.4)
# ---------------------------------------------------------------------------------------------------------------------

def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _when(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def binary_message(path: str) -> str:
    return f"{path} is binary; open it with open_file and file_ref workspace:{path}"


def render_view_header(entry: WorkspaceEntry, total_lines: int, *, version_view: bool = False) -> str:
    """Neutral (D17): path, generation, size, last writer and time — for history and debugging, not protection."""
    where = (
        f"version {entry.generation}, not current" if version_view and not entry.live
        else f"generation {entry.generation}"
    )
    fire = f" (fire {entry.fire})" if entry.fire else ""
    return (
        f"[workspace:{entry.path} · {where} · {total_lines} lines · {format_size(entry.size)}\n"
        f" last written {_when(entry.updated)} by {entry.origin or 'unknown'}{fire}]"
    )


def prior_generation_before_fire(versions: Sequence[WorkspaceEntry], fire: str) -> Optional[int]:
    """The newest version (live or noncurrent) written by anything but this fire (§7.4, B1)."""
    for v in sorted(versions, key=lambda e: e.generation, reverse=True):
        if v.fire != fire:
            return v.generation
    return None


def render_same_fire_line(prior_generation: Optional[int]) -> str:
    """§7.4 (B1): no state section is assumed (D16); the tool description says to work from the earlier version."""
    head = ("(this version was written by an earlier attempt of this same fire; its result may not have reached the "
            "user.\n")
    if prior_generation is None:
        return head + "The file did not exist before this fire.)"
    return head + f"The version before this fire is generation {prior_generation}.)"


def render_listing(prefix: str, entries: Sequence[WorkspaceEntry]) -> str:
    """A folder `view`: live files newest first (cap 100), deleted files counted (§6, §7.4)."""
    live = sorted((e for e in entries if e.live), key=lambda e: (e.updated, e.generation), reverse=True)
    deleted = {e.path for e in entries if not e.live} - {e.path for e in live}
    label = f"workspace:{prefix}" if prefix else "workspace (all folders)"
    lines = [f"{label} — {_count(len(live), 'file')}" if live else f"{label} — empty"]
    lines += [
        f"  {e.path} · {format_size(e.size)} · {_day(e.updated)} · {e.origin or 'unknown'}"
        for e in live[:LISTING_MAX_ENTRIES]
    ]
    if len(live) > LISTING_MAX_ENTRIES:
        lines.append(f"  …{len(live) - LISTING_MAX_ENTRIES} more")
    if deleted:
        where = f"in this folder — history {prefix} lists them" if prefix else "— history with an empty path lists them"
        lines.append(f"({_count(len(deleted), 'deleted file')} {where})")
    return "\n".join(lines)


def render_missing(path: str, versions: Sequence[WorkspaceEntry]) -> str:
    """A `view` of a path with no live object: who deleted it or where it went, with its versions (§7.4, D8)."""
    old = sorted((v for v in versions if v.path == path and not v.live), key=lambda v: v.generation, reverse=True)
    if not old:
        return f"workspace:{path} does not exist and never did."
    newest = old[0]
    when = _when(newest.deleted_at) if newest.deleted_at else "at an unknown time"
    by = f" by {newest.deleted_by}" if newest.deleted_by else ""
    if newest.renamed_to:
        second = (
            f"It was renamed to {newest.renamed_to} {('on ' + _when(newest.deleted_at)) if newest.deleted_at else 'at an unknown time'}{by}; update the reminder's workspace_file "
            f"(manage_self_reminders, context={{\"workspace_file\": \"{newest.renamed_to}\"}}) or rename it back."
        )
    else:
        second = f"It was deleted {when}{by}."
    lines = [f"workspace:{path} does not exist.", f"{second} Versions kept:"]
    lines += [
        f"  {v.generation} · {_when(v.updated)} · {v.origin or 'unknown'} · {format_size(v.size)}"
        for v in old[:LISTING_MAX_ENTRIES]
    ]
    if len(old) > LISTING_MAX_ENTRIES:
        lines.append(f"  …{len(old) - LISTING_MAX_ENTRIES} more")
    lines.append("Tell the user it is missing and offer `restore` with one of these generations. Do not recreate it.")
    return "\n".join(lines)


def _status(v: WorkspaceEntry) -> str:
    if v.live:
        return "current"
    when = f" {_when(v.deleted_at)}" if v.deleted_at else ""
    by = f" by {v.deleted_by}" if v.deleted_by else ""
    if v.renamed_to:
        return f"renamed to {v.renamed_to}{by}{when}"
    if v.deleted_by:
        return f"deleted{by}{when}"
    return f"replaced{when}"


def render_history(path: str, entries: Sequence[WorkspaceEntry]) -> str:
    """Versions newest first: generation, time, size, origin, fire, status (§7.3). A folder lists deleted files too."""
    if path == "" or path.endswith("/"):
        return _render_folder_history(path, entries)
    versions = sorted((e for e in entries if e.path == path), key=lambda e: e.generation, reverse=True)
    if not versions:
        return f"workspace:{path} has no history: it never existed."
    lines = [f"history of workspace:{path} — {_count(len(versions), 'version')}"]
    for v in versions[:LISTING_MAX_ENTRIES]:
        fire = f" · fire {v.fire}" if v.fire else ""
        restored = f" · restored from {v.restored_from}" if v.restored_from else ""
        lines.append(
            f"  {v.generation} · {_when(v.updated)} · {format_size(v.size)} · {v.origin or 'unknown'}"
            f"{fire}{restored} · {_status(v)}"
        )
    if len(versions) > LISTING_MAX_ENTRIES:
        lines.append(f"  …{len(versions) - LISTING_MAX_ENTRIES} more")
    return "\n".join(lines)


def _render_folder_history(prefix: str, entries: Sequence[WorkspaceEntry]) -> str:
    newest: Dict[str, WorkspaceEntry] = {}
    counts: Dict[str, int] = {}
    for e in entries:
        counts[e.path] = counts.get(e.path, 0) + 1
        if e.path not in newest or e.generation > newest[e.path].generation:
            newest[e.path] = e
    label = f"workspace:{prefix}" if prefix else "workspace (all folders)"
    if not newest:
        return f"history of {label} — no files, now or before"
    rows = sorted(newest.values(), key=lambda e: (e.updated, e.generation), reverse=True)
    lines = [f"history of {label} — {_count(len(rows), 'file')}"]
    for e in rows[:LISTING_MAX_ENTRIES]:
        state = f"current, {format_size(e.size)}" if e.live else _status(e)
        lines.append(
            f"  {e.path} · {_count(counts[e.path], 'version')} · {state} · last written {_when(e.updated)} "
            f"by {e.origin or 'unknown'}"
        )
    if len(rows) > LISTING_MAX_ENTRIES:
        lines.append(f"  …{len(rows) - LISTING_MAX_ENTRIES} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------------
# References (§8.4) and one command's input / output (§7.6)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceReferences:
    """What this run can see that may name a workspace path: loaded skills' (name, body) and the Router's
    `agent_notes` dicts (present on Router-routed runs; each carries `workspace_file`). Used only for delete / rename
    warnings (§8.4)."""

    skills: Tuple[Tuple[str, str], ...] = ()
    notes: Tuple[Mapping[str, Any], ...] = ()


def find_references(path: str, refs: WorkspaceReferences, new_path: Optional[str] = None) -> List[str]:
    warnings = [f'Warning: skill "{name}" names this path' for name, body in refs.skills if path in body]
    target = new_path or "<new path>"
    for note in refs.notes:
        named = str(note.get("workspace_file") or "")
        if named and named.removeprefix(WORKSPACE_REF_PREFIX) == path:
            warnings.append(
                f'Warning: reminder "{note.get("text", "")}" [{note.get("note_id", "")}] uses this workspace file; '
                f'update it (manage_self_reminders, context={{"workspace_file": "{target}"}}) or restore the file.'
            )
    return warnings


@dataclass(frozen=True)
class WorkspaceCall:
    """One command's context, built by the per-execution handler: who runs it, which fire, and the generations this
    execution has seen (a `create` over an existing path needs one, §7.3)."""

    user_id: str
    origin: str
    fire_key: Optional[str] = None
    known: Mapping[str, int] = field(default_factory=dict)
    references: WorkspaceReferences = field(default_factory=WorkspaceReferences)


@dataclass(frozen=True)
class WorkspaceResult:
    """Model-facing text and the session-map updates the handler applies (None removes a path)."""

    text: str
    failed: bool = False
    generations: Mapping[str, Optional[int]] = field(default_factory=dict)
