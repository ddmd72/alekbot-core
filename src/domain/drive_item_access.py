"""What the provider lets the bot do with one item (MULTI_DRIVE_RFC §7.2, §11.3 row 4). Adapter-built and carried on
`DriveItem.access`; every flag is True for an item of an App Folder (the bot's own area)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemAccess:
    can_download: bool = True
    can_write_content: bool = True
    can_rename_move: bool = True
    can_delete: bool = True
    can_add_children: bool = True
