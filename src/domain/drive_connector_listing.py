"""The drive MCP connector's `/` listing and its drive-top head (DRIVE_MCP_CONNECTOR_RFC §7.4, E10b; slice 3 DELTA 2).

Code-built and neutral, like every other listing line (`drive_mounts.render_mount_line`): each visible drive shows its
connector access, never its write folder, its search scope or a writable / read-only part, which the connector has no
use for. The heading is chat's `MOUNT_LISTING_HEADING`.

Public interface:
    CONNECTOR_TOPS_HEAD
    ConnectorMountRow (frozen)
    connector_mount_row(mount, search, *, note, folder_paths, more) -> ConnectorMountRow
    render_connector_mount_listing(rows) -> str
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .drive_connection_status import DriveConnectionStatus
from .drive_line import DriveLine
from .drive_mount import Mount
from .drive_mounts import MOUNT_LISTING_HEADING, canonical_path, drive_note_line
from .drive_search_line import SearchLine

CONNECTOR_TOPS_HEAD = "Folders shared with this connector on {drive}:"


@dataclass(frozen=True)
class ConnectorMountRow:
    name: str
    primary: bool
    drive: DriveLine
    search: Optional[SearchLine]
    whole: bool                      # the connector reads everything the bot sees on the drive
    folders: Tuple[str, ...]         # canonical paths of the connector folders, at most the caller's limit
    more: int                        # how many connector folders the row does not name
    searched_by_default: bool
    note: str                        # the owner's drive note, already bounded


def connector_mount_row(mount: Mount, search: Optional[SearchLine], *, note: str, folder_paths: Sequence[str],
                        more: int) -> ConnectorMountRow:
    """The row of one visible mount: `folder_paths` are the connector folders' connection-relative paths (read by the
    caller, `UserDriveService.connector_folder_paths`); `note` the owner's drive note, bounded here."""
    connection = mount.connection
    access = connection.connector_access
    return ConnectorMountRow(name=mount.name, primary=mount.primary,
                             drive=DriveLine.of(DriveConnectionStatus.of(connection)), search=search,
                             whole=access is not None and access.whole,
                             folders=tuple(canonical_path(mount.name, p) for p in folder_paths), more=max(0, more),
                             searched_by_default=connection.searched_by_default,
                             note=drive_note_line(note) if note else "")


def _render(row: ConnectorMountRow) -> str:
    parts: List[str] = []
    if row.primary:
        parts.append("primary")
    parts.append(row.drive.value)
    if row.search is not None:
        parts.append(f"search {row.search.value}")
    if row.whole:
        parts.append("connector reads everything the bot sees on it")
    elif row.folders:
        more = f" +{row.more} more" if row.more > 0 else ""
        parts.append(f"connector reads only {', '.join(row.folders)}{more}")
    else:
        parts.append("connector reads chosen folders (their paths could not be read)")
    parts.append("searched by default" if row.searched_by_default else "searched only when named")
    if row.note:
        parts.append(f'owner\'s note: "{row.note}"')
    return f"- {row.name}: " + "; ".join(parts)


def render_connector_mount_listing(rows: Sequence[ConnectorMountRow]) -> str:
    """The connector's `/` listing: chat's heading, then one line per visible mount."""
    return "\n".join([MOUNT_LISTING_HEADING, *(_render(row) for row in rows)])
