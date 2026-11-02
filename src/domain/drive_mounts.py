"""The mount tree: names, paths and the `drives` value (MULTI_DRIVE_RFC §11.1, §11.2, §11.8; slice 2a).

Every path the model or the user reads starts with its drive's mount name (`Drive/R&D/Tom's plan.docx`); inside the
code a path stays connection-relative. `resolve_path` turns a written path into (mount, relative path); a path without
a mount name is on the primary (or a given default). A mount name shadows a primary top-level folder of the same name.
A renamed drive answers to its former name for `HOLD` after the rename, with `RENAME_NOTICE`.

Names are compared by `mount_name_key` (NFC, trimmed, inner whitespace collapsed, case-folded). `validate_mount_name`
holds every naming rule of §11.8; its only caller is the Cabinet's name field (slice 2b).

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
    mount_line(mount, search) -> MountLine
    render_mount_line(line) -> str
    render_mount_listing(lines) -> str
    Texts: RENAME_NOTICE, UNKNOWN_DRIVE_LINE, ONE_DRIVE_LINE, CROSS_DRIVE_LINE, READ_ONLY_LINE, FOLDER_DRIVE_MISMATCH,
    NAME_EMPTY, NAME_TOO_LONG, NAME_FORBIDDEN_CHARS, NAME_DOT, NAME_RESERVED, NAME_TAKEN, NAME_HELD, SHADOW_WARNING,
    MOUNT_LISTING_HEADING
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set, Tuple, Union

from .drive_connection import DEFAULT_MOUNT_NAME, DriveConnection
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
    "resolve_drives", "unknown_drive_line", "default_mount_name", "validate_mount_name", "mount_line",
    "render_mount_line", "render_mount_listing",
    "RENAME_NOTICE", "UNKNOWN_DRIVE_LINE", "ONE_DRIVE_LINE", "CROSS_DRIVE_LINE", "READ_ONLY_LINE",
    "FOLDER_DRIVE_MISMATCH", "NAME_EMPTY", "NAME_TOO_LONG", "NAME_FORBIDDEN_CHARS", "NAME_DOT", "NAME_RESERVED",
    "NAME_TAKEN", "NAME_HELD", "SHADOW_WARNING", "MOUNT_LISTING_HEADING",
]

HOLD = timedelta(days=30)
MAX_NAME_CHARS = 40
FORBIDDEN_NAME_CHARS = '/\\:*?"<>|'
RESERVED_NAMES = ("_Transcripts", "Archive", "Long paths", "Inbox")
TREE_ROOT = "/"

# Texts (English, code-built; no provider names)
RENAME_NOTICE = "{former} is now called {current}."
UNKNOWN_DRIVE_LINE = 'There is no drive named "{name}". The drives are: {names}.'
ONE_DRIVE_LINE = "Name one drive per search for now. The drives are: {names}."
CROSS_DRIVE_LINE = "Moving or copying a file between drives is not available yet."
READ_ONLY_LINE = "Only the primary drive can be changed for now; this drive is read-only."
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
    else by a held former name (with RENAME_NOTICE); otherwise the whole path is on `default`, else the primary. A mount
    shadows a primary top-level folder of the same name. DrivePathError for `\\`, `.` or `..`; DriveNotConnectedError
    when the path names no mount and there is neither a default nor a primary."""
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
    if target is None:
        raise DriveNotConnectedError("no drive to resolve the path in")
    return PathTarget(mount=target, path="/".join(segments), named=False)


# ---------------------------------------------------------------------------------------------- `drives`

def _names(table: MountTable) -> str:
    return ", ".join(table.names())


def unknown_drive_line(name: str, table: MountTable) -> str:
    return UNKNOWN_DRIVE_LINE.format(name=name, names=_names(table))


def resolve_drives(table: MountTable, value: Optional[str]) -> Union[DriveSelection, str]:
    """The mounts a search reads (2a: one), or the refusal line.
    - empty: the primary (else the oldest live mount; none: no mount);
    - one name: that mount by current or held former name (a former name adds RENAME_NOTICE); unknown: the
      UNKNOWN_DRIVE_LINE refusal;
    - two or more names (comma-separated, empties dropped, de-duplicated by key): the ONE_DRIVE_LINE refusal.
    `not_searched` lists the current names of the other live mounts, table order."""
    wanted: Dict[str, str] = {}
    for piece in (value or "").split(","):
        name = piece.strip()
        if name and mount_name_key(name) not in wanted:
            wanted[mount_name_key(name)] = name
    if len(wanted) > 1:
        return ONE_DRIVE_LINE.format(names=_names(table))
    notice = ""
    if not wanted:
        chosen = table.primary() or (table.mounts[0] if table.mounts else None)
        if chosen is None:
            return DriveSelection(mounts=())
    else:
        name = next(iter(wanted.values()))
        found = table.by_name(name)
        if found is None:
            return unknown_drive_line(name, table)
        chosen, via_former = found
        if via_former:
            key = mount_name_key(name)
            former = next(n for n in chosen.former_names if mount_name_key(n) == key)
            notice = RENAME_NOTICE.format(former=former, current=chosen.name)
    others = tuple(m.name for m in table.mounts if m.connection_id != chosen.connection_id)
    return DriveSelection(mounts=(chosen,), not_searched=others, notice=notice)


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


# ---------------------------------------------------------------------------------------------- lines

def mount_line(mount: Mount, search: Optional[SearchLine]) -> MountLine:
    """The line of one mount; 2a: writable and searched by default exactly when primary."""
    return MountLine(name=mount.name, primary=mount.primary,
                     drive=DriveLine.of(DriveConnectionStatus.of(mount.connection)), search=search,
                     writable=mount.primary, searched_by_default=mount.primary)


def render_mount_line(line: MountLine) -> str:
    """`- Drive: primary; connected; search on; writable; searched by default` (no search part when `search` is None)."""
    parts: List[str] = []
    if line.primary:
        parts.append("primary")
    parts.append(line.drive.value)
    if line.search is not None:
        parts.append(f"search {line.search.value}")
    parts.append("writable" if line.writable else "read-only")
    parts.append("searched by default" if line.searched_by_default else "searched only when named")
    return f"- {line.name}: " + "; ".join(parts)


def render_mount_listing(lines: Sequence[MountLine]) -> str:
    """The `/` listing: the heading, then one line per mount."""
    return "\n".join([MOUNT_LISTING_HEADING, *(render_mount_line(line) for line in lines)])
