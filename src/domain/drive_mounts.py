"""The mount tree: names, paths and the `drives` value (MULTI_DRIVE_RFC §11.1, §11.2, §11.8, §11.10; slices 2a, 2b).

Every path the model or the user reads starts with its drive's mount name (`Drive/R&D/Tom's plan.docx`); inside the
code a path stays connection-relative. `resolve_path` turns a written path into (mount, relative path); a path without
a mount name is on the primary (or a given default; with neither, the one live mount when there is exactly one). A
mount name shadows a primary top-level folder of the same name.
A renamed drive answers to its former name for `HOLD` after the rename, with `RENAME_NOTICE`.

Names are compared by `mount_name_key` (NFC, trimmed, inner whitespace collapsed, case-folded). `validate_mount_name`
holds every naming rule of §11.8; its caller is `DriveConnectionService.rename` (the Cabinet's name field), inside the
repository transaction; `renamed_history` is the history a rename stores.

Public interface:
    HOLD, MAX_NAME_CHARS, FORBIDDEN_NAME_CHARS, RESERVED_NAMES, TREE_ROOT
    mount_name_key(name) -> str
    held_former_names(history, current, now) -> Tuple[str, ...]
    mount_table(connections, primary_connection_id, now) -> MountTable
    canonical_path(mount_name, path) -> str
    is_tree_root(path) -> bool
    resolve_path(table, path, *, default=None) -> PathTarget        DrivePathError, DriveNotConnectedError
    resolve_drives(table, value) -> DriveSelection | str
    unknown_drive_line(name, table) -> str
    default_mount_name(connections, now) -> str
    validate_mount_name(raw, *, connection_id, connections, primary_connection_id, primary_top_folders, now)
        -> MountNameCheck
    renamed_history(connection, name, at) -> Tuple[MountNameChange, ...]
    mount_line(mount, search, *, write_folder_path=None, note="", scope_paths=(), scope_more=0) -> MountLine
    render_mount_line(line) -> str
    render_mount_listing(lines) -> str
    drive_note_line(text) -> str;  NOTE_LINE_MAX
    Texts: RENAME_NOTICE, UNKNOWN_DRIVE_LINE, CROSS_DRIVE_LINE, FOLDER_DRIVE_MISMATCH,
    NAME_EMPTY, NAME_TOO_LONG, NAME_FORBIDDEN_CHARS, NAME_DOT, NAME_RESERVED, NAME_TAKEN, NAME_HELD, SHADOW_WARNING,
    MOUNT_LISTING_HEADING, PRIMARY_NEEDS_WRITE_FOLDER_LINE, NO_PRIMARY_LINE, HEIR_INVALID_LINE
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from .drive_connection import DEFAULT_MOUNT_NAME, ROOT_FOLDER, DriveConnection
from .drive_connection_state import ConnectionState
from .drive_connection_status import DriveConnectionStatus
from .drive_line import DriveLine
from .drive_mount import Mount
from .drive_mount_line import MountLine
from .drive_mount_name_change import MountNameChange, mount_name_key, normalise_mount_name
from .drive_mount_name_check import MountNameCheck
from .drive_mount_table import MountTable
from .drive_path_target import PathTarget
from .drive_search_line import SearchLine
from .drive_selection import DriveSelection
from .user_drive import DriveNotConnectedError, split_folder_path

__all__ = [
    "HOLD", "MAX_NAME_CHARS", "FORBIDDEN_NAME_CHARS", "RESERVED_NAMES", "TREE_ROOT",
    "mount_name_key", "held_former_names", "mount_table", "canonical_path", "is_tree_root", "resolve_path",
    "resolve_drives", "unknown_drive_line", "default_mount_name", "validate_mount_name", "renamed_history",
    "mount_line", "render_mount_line", "render_mount_listing", "drive_note_line", "NOTE_LINE_MAX",
    "RENAME_NOTICE", "UNKNOWN_DRIVE_LINE", "CROSS_DRIVE_LINE",
    "FOLDER_DRIVE_MISMATCH", "NAME_EMPTY", "NAME_TOO_LONG", "NAME_FORBIDDEN_CHARS", "NAME_DOT", "NAME_RESERVED",
    "NAME_TAKEN", "NAME_HELD", "SHADOW_WARNING", "MOUNT_LISTING_HEADING", "PRIMARY_NEEDS_WRITE_FOLDER_LINE",
    "NO_PRIMARY_LINE", "HEIR_INVALID_LINE",
]

HOLD = timedelta(days=30)
MAX_NAME_CHARS = 40
FORBIDDEN_NAME_CHARS = '/\\:*?"<>|'
RESERVED_NAMES = ("_Transcripts", "Archive", "Long paths", "Inbox")
TREE_ROOT = "/"
NOTE_LINE_MAX = 200          # §11.10: the drive note's preflight line is bounded to its first 200 characters

# Texts (English, code-built; no provider names)
RENAME_NOTICE = "{former} is now called {current}."
UNKNOWN_DRIVE_LINE = 'There is no drive named "{name}". The drives are: {names}.'
CROSS_DRIVE_LINE = "Moving or copying a file between drives is not available yet."
NO_TOP_LEVEL_DESTINATION_LINE = "Name a folder on Drive: a drive's top level cannot be a destination."
PRIMARY_NEEDS_WRITE_FOLDER_LINE = "Only a connected drive with a write folder can be the primary."
NO_PRIMARY_LINE = "No drive is the primary yet: the owner chooses one in the Cabinet."
HEIR_INVALID_LINE = "The new primary must be another connected drive with a write folder."
FOLDER_DRIVE_MISMATCH = "The folder {folder} is on {mount_a}, not on {mount_b}."
NAME_EMPTY = "A drive name is required."
NAME_TOO_LONG = "A drive name can have at most 40 characters."
NAME_FORBIDDEN_CHARS = 'A drive name cannot contain / \\ : * ? " < > | or control characters.'
NAME_DOT = "A drive name cannot start or end with a dot."
NAME_RESERVED = "{name} is the name of a folder the bot manages; choose another name."
NAME_TAKEN = "Another drive is already called {name}."
NAME_HELD = "{name} was another drive's name until recently; it stays reserved for 30 days after a rename."
SHADOW_WARNING = "The folder {name} in {primary} will be reached as {primary}/{name}."
MOUNT_LISTING_HEADING = "Drives (a path without a drive name is on the primary drive):"

_LIVE = (ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT)
_RESERVED_KEYS = frozenset(mount_name_key(n) for n in RESERVED_NAMES)
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------------------------- the tree

def held_former_names(history: Sequence[MountNameChange], current: str, now: datetime) -> Tuple[str, ...]:
    """The former names still held at `now`, newest first. Entry i (not the last) was left at `history[i+1].at` and is
    held while `now < left_at + HOLD`. A name equal (key) to `current` is skipped; a name taken twice counts once, by
    its newest leaving."""
    current_key = mount_name_key(current)
    held: List[str] = []
    seen: Set[str] = {current_key}
    for i in range(len(history) - 2, -1, -1):
        key = mount_name_key(history[i].name)
        if key in seen:
            continue
        seen.add(key)
        if now < history[i + 1].at + HOLD:
            held.append(history[i].name)
    return tuple(held)


def mount_table(connections: Sequence[DriveConnection], primary_connection_id: Optional[str],
                now: datetime) -> MountTable:
    """The live (ACTIVE, NEEDS_RECONNECT) connections in the given order (the repository's: oldest first); the primary
    is the live connection `primary_connection_id` names."""
    return MountTable(mounts=tuple(
        Mount(connection=c, primary=primary_connection_id is not None and c.connection_id == primary_connection_id,
              former_names=held_former_names(c.name_history, c.mount_name, now))
        for c in connections if c.state in _LIVE))


def canonical_path(mount_name: str, path: str) -> str:
    """`<mount>/<path>` for a connection-relative `path` ("" = the mount itself). Callers pass relative paths: a path
    already prefixed is prefixed again."""
    relative = path.strip("/")
    return f"{mount_name}/{relative}" if relative else mount_name


def is_tree_root(path: Optional[str]) -> bool:
    """`/`, the tree of mounts. An empty path is not the tree root (it is the primary's top level)."""
    return path is not None and path.strip() == TREE_ROOT


def resolve_path(table: MountTable, path: Optional[str], *, default: Optional[Mount] = None) -> PathTarget:
    """The mount and connection-relative path of a written path. The first segment names a mount by its current name,
    else by a held former name (with RENAME_NOTICE); otherwise the whole path is on `default`, else the primary, else
    the one live mount when the table holds exactly one (E-7: a first drive is not primary until its write folder is
    chosen). A mount shadows a primary top-level folder of the same name. DrivePathError for `\\`, `.` or `..`;
    DriveNotConnectedError when the path names no mount and none of those exists."""
    segments = split_folder_path(path or "")
    if segments:
        found = table.by_name(segments[0])
        if found is not None:
            mount, via_former = found
            notice = ""
            if via_former:
                key = mount_name_key(segments[0])
                former = next(n for n in mount.former_names if mount_name_key(n) == key)
                notice = RENAME_NOTICE.format(former=former, current=mount.name)
            return PathTarget(mount=mount, path="/".join(segments[1:]), named=True, notice=notice)
    target = default if default is not None else table.primary()
    if target is None and len(table.mounts) == 1:
        target = table.mounts[0]
    if target is None:
        raise DriveNotConnectedError("no drive to resolve the path in")
    return PathTarget(mount=target, path="/".join(segments), named=False)


# ---------------------------------------------------------------------------------------------- `drives`

def _names(table: MountTable) -> str:
    return ", ".join(table.names())


def unknown_drive_line(name: str, table: MountTable) -> str:
    return UNKNOWN_DRIVE_LINE.format(name=name, names=_names(table))


def resolve_drives(table: MountTable, value: Optional[str]) -> Union[DriveSelection, str]:
    """The mounts a search reads, or the refusal line (D4; E43 dropped in 2b).
    - empty: every live mount searched by default (table order; NEEDS_RECONNECT included, its gate answers); when none
      is, the primary, else the oldest live mount (E-6: an empty value never reads nothing while a mount exists);
    - names (comma-separated, empties dropped, de-duplicated by key): those mounts, table order, each by current or
      held former name (each former name adds its RENAME_NOTICE, joined by newlines); the first unknown name: the
      UNKNOWN_DRIVE_LINE refusal.
    `not_searched` lists the current names of the other live mounts, table order."""
    wanted: Dict[str, str] = {}
    for piece in (value or "").split(","):
        name = piece.strip()
        if name and mount_name_key(name) not in wanted:
            wanted[mount_name_key(name)] = name
    notices: List[str] = []
    chosen_ids: Set[str] = set()
    if not wanted:
        flagged = [m for m in table.mounts if m.connection.searched_by_default]
        fallback = table.primary() or (table.mounts[0] if table.mounts else None)
        chosen_ids = {m.connection_id for m in flagged} or ({fallback.connection_id} if fallback else set())
    else:
        for key, name in wanted.items():
            found = table.by_name(name)
            if found is None:
                return unknown_drive_line(name, table)
            mount, via_former = found
            chosen_ids.add(mount.connection_id)
            if via_former:
                former = next(n for n in mount.former_names if mount_name_key(n) == key)
                notices.append(RENAME_NOTICE.format(former=former, current=mount.name))
    chosen = tuple(m for m in table.mounts if m.connection_id in chosen_ids)
    others = tuple(m.name for m in table.mounts if m.connection_id not in chosen_ids)
    return DriveSelection(mounts=chosen, not_searched=others, notice="\n".join(notices))


# ---------------------------------------------------------------------------------------------- names

def _taken_keys(connections: Sequence[DriveConnection], now: datetime, *,
                except_connection: Optional[str] = None) -> Tuple[Set[str], Set[str]]:
    """(current-name keys, held-former-name keys) of the live connections other than `except_connection`."""
    current: Set[str] = set()
    held: Set[str] = set()
    for c in connections:
        if c.state not in _LIVE or c.connection_id == except_connection:
            continue
        current.add(mount_name_key(c.mount_name))
        held.update(mount_name_key(n) for n in held_former_names(c.name_history, c.mount_name, now))
    return current, held


def default_mount_name(connections: Sequence[DriveConnection], now: datetime) -> str:
    """The lowest of `Drive`, `Drive 2`, `Drive 3`, … that no live connection is called or holds as a former name.
    A DISCONNECTED connection's names are free."""
    current, held = _taken_keys(connections, now)
    taken = current | held
    if mount_name_key(DEFAULT_MOUNT_NAME) not in taken:
        return DEFAULT_MOUNT_NAME
    n = 2
    while mount_name_key(f"{DEFAULT_MOUNT_NAME} {n}") in taken:
        n += 1
    return f"{DEFAULT_MOUNT_NAME} {n}"


def _has_forbidden(name: str) -> bool:
    return any(ch in FORBIDDEN_NAME_CHARS or unicodedata.category(ch) in ("Cc", "Cf") for ch in name)


def validate_mount_name(raw: str, *, connection_id: Optional[str], connections: Sequence[DriveConnection],
                        primary_connection_id: Optional[str], primary_top_folders: Sequence[str],
                        now: datetime) -> MountNameCheck:
    """Every naming rule of §11.8, first failure wins: empty, too long, forbidden or control characters, a leading or
    trailing dot, a bot-managed folder name, another live connection's current name, another live connection's held
    former name. `connection_id` is the connection being named (None for a new one); its own former names may come
    back. A valid name equal to one of `primary_top_folders` (the primary's top-level folder names) carries the
    shadowing warning; the primary is `primary_connection_id`, its name the new one when it is the one renamed."""
    name = normalise_mount_name(raw)
    if not name:
        return MountNameCheck(name=name, error=NAME_EMPTY)
    if len(name) > MAX_NAME_CHARS:
        return MountNameCheck(name=name, error=NAME_TOO_LONG)
    if _has_forbidden(name):
        return MountNameCheck(name=name, error=NAME_FORBIDDEN_CHARS)
    if name.startswith(".") or name.endswith("."):
        return MountNameCheck(name=name, error=NAME_DOT)
    key = mount_name_key(name)
    if key in _RESERVED_KEYS:
        return MountNameCheck(name=name, error=NAME_RESERVED.format(name=name))
    current, held = _taken_keys(connections, now, except_connection=connection_id)
    if key in current:
        return MountNameCheck(name=name, error=NAME_TAKEN.format(name=name))
    if key in held:
        return MountNameCheck(name=name, error=NAME_HELD.format(name=name))
    warning = ""
    if key in {mount_name_key(f) for f in primary_top_folders}:
        primary_name = _primary_name(connections, primary_connection_id, connection_id, name)
        if primary_name:
            warning = SHADOW_WARNING.format(name=name, primary=primary_name)
    return MountNameCheck(name=name, warning=warning)


def _primary_name(connections: Sequence[DriveConnection], primary_connection_id: Optional[str],
                  connection_id: Optional[str], new_name: str) -> str:
    if primary_connection_id is None:
        return ""
    if primary_connection_id == connection_id:
        return new_name
    primary = next((c for c in connections
                    if c.connection_id == primary_connection_id and c.state in _LIVE), None)
    return primary.mount_name if primary is not None else ""


def renamed_history(connection: DriveConnection, name: str, at: datetime) -> Tuple[MountNameChange, ...]:
    """The `name_history` a rename to `name` at `at` stores: the stored history plus `{name, at}`. A connection stored
    before the history existed first gets its current name at its creation instant, so the name it leaves is held."""
    history = connection.name_history or (MountNameChange(name=connection.mount_name, at=connection.created_at),)
    return (*history, MountNameChange(name=name, at=at))


# ---------------------------------------------------------------------------------------------- lines

def drive_note_line(text: str) -> str:
    """The owner's drive note as one preflight line (§11.10): whitespace runs to one space, double quotes to single
    (the line quotes it), cut to NOTE_LINE_MAX characters plus `…` when longer."""
    line = _WHITESPACE.sub(" ", text).strip().replace('"', "'")
    return line if len(line) <= NOTE_LINE_MAX else line[:NOTE_LINE_MAX] + "…"


def mount_line(mount: Mount, search: Optional[SearchLine], *, write_folder_path: Optional[str] = None,
               note: str = "", scope_paths: Sequence[str] = (), scope_more: int = 0) -> MountLine:
    """The line of one mount: writable when the connection has a write folder; `write_folder_path` (the write folder's
    connection-relative path, read by the caller) prints a write folder below the root; `note` is the owner's drive
    note, bounded here; `scope_paths` (the chosen folders' connection-relative paths, G11) and `scope_more` (how many
    more there are) print `indexes only ...`."""
    connection = mount.connection
    folder = connection.write_folder_id
    write_folder = ""
    below_root = folder is not None and folder != ROOT_FOLDER
    if below_root and write_folder_path:
        write_folder = canonical_path(mount.name, write_folder_path)
    return MountLine(name=mount.name, primary=mount.primary,
                     drive=DriveLine.of(DriveConnectionStatus.of(connection)), search=search,
                     writable=folder is not None, searched_by_default=connection.searched_by_default,
                     write_folder=write_folder, write_folder_unknown=below_root and not write_folder_path,
                     note=drive_note_line(note) if note else "",
                     scope_folders=tuple(canonical_path(mount.name, p) for p in scope_paths), scope_more=scope_more)


def render_mount_line(line: MountLine) -> str:
    """`- Drive: primary; connected; search on; writable; searched by default` (no search part when `search` is None);
    a write folder below the root prints `writable only in <path>`; the owner's note ends the line as
    `owner's note: "<note>"`; chosen folders print `indexes only <p1>, <p2>, ... +N more` after the writable part."""
    parts: List[str] = []
    if line.primary:
        parts.append("primary")
    parts.append(line.drive.value)
    if line.search is not None:
        parts.append(f"search {line.search.value}")
    if not line.writable:
        parts.append("read-only")
    else:
        if line.write_folder:
            parts.append(f"writable only in {line.write_folder}")
        elif line.write_folder_unknown:
            parts.append("writes go to a chosen folder (its path could not be read)")
        else:
            parts.append("writable")
    if line.scope_folders:
        more = f" +{line.scope_more} more" if line.scope_more > 0 else ""
        parts.append(f"indexes only {', '.join(line.scope_folders)}{more}")
    parts.append("searched by default" if line.searched_by_default else "searched only when named")
    if line.note:
        parts.append(f'owner\'s note: "{line.note}"')
    return f"- {line.name}: " + "; ".join(parts)


def render_mount_listing(lines: Sequence[MountLine]) -> str:
    """The `/` listing: the heading, then one line per mount."""
    return "\n".join([MOUNT_LISTING_HEADING, *(render_mount_line(line) for line in lines)])
