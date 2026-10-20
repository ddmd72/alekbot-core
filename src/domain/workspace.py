"""Agent workspace — Smart's own persistent working files (docs/10_rfcs/AGENT_WORKSPACE_RFC.md).

Pure: the path rule, kinds, caps, reminder-field values, line windows, exact-string replacement and every text the
`workspace` tool shows. No I/O: WorkspaceService (services/) does the work through WorkspaceStorePort.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

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
    if text.startswith("drive:"):
        raise WorkspaceRefused(
            f"'{text}' is on the user's drive, not in the workspace; open it with open_file_from_drive",
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
            f"to keep a chat file, use copy with source \"{text}\" and a path under files/",
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
    if not text.isdigit():
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
