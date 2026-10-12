"""
FixtureDrive — a UserDrivePort over the eval manifest (tests/fixtures/drive_eval/tree.json).

Serves the indexing reads only (USER_DRIVE_INDEXING_RFC §4). The first `get_changes` call pages a full
enumeration in parent-first order; the cursor it ends with yields delta rounds with no real change from
then on. As in S0a, every round carries the App Folder root itself first. Ids are a hash of the path, so
they are stable across runs and recorded bars always mean the same items. Facets keep Graph's raw shape
(S0e: `photo` may be `{}`, audio has no facet) with `file.hashes` lifted to `hashes`, where the domain
reads it (`drive_card.content_hash_of`). Folder listings are paged like Graph's (`limit`, opaque cursor).
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.user_drive import (
    DriveConnection, DriveItem, DriveItemNotFoundError, DriveResyncRequired,
)
from src.ports.user_drive_port import UserDrivePort

ROOT_ID = "fx-root"
DRIVE_ID = "fx-drive"
DEFAULT_FS_MODIFIED = "2026-01-15T09:00:00Z"   # generated entries without their own date
_FULL = "fx:full:"
_DELTA = "fx:delta"
_CHILDREN = "fx:children:"
_ROOT_CHANGE = DriveChange(item_id=ROOT_ID, name="root", is_folder=True)   # S0a: in every round, no `root` facet
_RAW_FACETS = ("photo", "location", "image", "video", "audio", "remoteItem", "package", "malware")


def item_id_for(path: str) -> str:
    return "fx-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _format_strings(value: Any, n: int) -> Any:
    """`{n}` placeholders in a generate entry's facets_template, at any depth."""
    if isinstance(value, str):
        return value.format(n=n)
    if isinstance(value, dict):
        return {k: _format_strings(v, n) for k, v in value.items()}
    if isinstance(value, list):
        return [_format_strings(v, n) for v in value]
    return value


def _facets(raw: Mapping[str, Any]) -> Dict[str, Any]:
    out = {key: copy.deepcopy(raw[key]) for key in _RAW_FACETS if key in raw}
    hashes = (raw.get("file") or {}).get("hashes")
    if hashes:
        out["hashes"] = dict(hashes)
    return out


class FixtureDrive(UserDrivePort):
    def __init__(self, manifest: Mapping[str, Any], page_size: int = 200) -> None:
        self.page_size = page_size
        self._items: Dict[str, DriveChange] = {}
        self._paths: Dict[str, str] = {}                 # item_id -> path
        for entry in manifest.get("items", []):
            self._add(entry["path"], bool(entry.get("folder")), entry)
        for gen in manifest.get("generate", []):
            for n in range(int(gen["count"])):
                path = f"{gen['folder']}/{gen['pattern'].format(n=n)}"
                self._add(path, False, {
                    "mime": gen.get("mime", ""), "size": gen.get("size", 0),
                    "fs_modified": gen.get("fs_modified", DEFAULT_FS_MODIFIED),
                    "facets": _format_strings(gen.get("facets_template") or {}, n),
                })
        counts: Dict[str, int] = {}
        for change in self._items.values():
            counts[change.parent_id] = counts.get(change.parent_id, 0) + 1
        for item_id, change in list(self._items.items()):
            if change.is_folder:
                self._items[item_id] = replace(change, child_count=counts.get(item_id, 0))
        self._order = sorted(self._items, key=lambda i: self._paths[i].split("/"))

    @classmethod
    def from_file(cls, path: Path, page_size: int = 200) -> "FixtureDrive":
        return cls(json.loads(path.read_text(encoding="utf-8")), page_size)

    # -- manifest -> observations ----------------------------------------------------------------

    def _ensure_folder(self, path: str) -> str:
        if not path:
            return ROOT_ID
        item_id = item_id_for(path)
        if item_id not in self._items:
            self._add(path, True, {})
        return item_id

    def _add(self, path: str, folder: bool, entry: Mapping[str, Any]) -> None:
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        parent_id = self._ensure_folder(parent_path)
        item_id = item_id_for(path)
        modified = _parse_dt(entry.get("fs_modified") or DEFAULT_FS_MODIFIED)
        self._paths[item_id] = path
        self._items[item_id] = DriveChange(
            item_id=item_id, parent_id=parent_id, name=name, is_folder=folder,
            size_bytes=0 if folder else int(entry.get("size", 0)),
            mime_type="" if folder else str(entry.get("mime", "")),
            content_version="" if folder else f"ctag-{item_id}",
            modified_at=modified, fs_modified_at=modified, facets=_facets(entry.get("facets") or {}),
        )

    # -- lookups for the runner ------------------------------------------------------------------

    def path_of(self, item_id: str) -> Optional[str]:
        return self._paths.get(item_id)

    def paths(self) -> List[str]:
        return [self._paths[i] for i in self._order]

    # -- indexing reads (USER_DRIVE_INDEXING_RFC §4) -------------------------------------------

    async def get_changes(self, user_id: str, cursor: Optional[str]) -> DriveChanges:
        if cursor == _DELTA:
            return DriveChanges((_ROOT_CHANGE,), _DELTA, True, False, DRIVE_ID, ROOT_ID)
        if cursor is not None and not cursor.startswith(_FULL):
            raise DriveResyncRequired(f"{_FULL}0")
        start = int(cursor[len(_FULL):]) if cursor else 0
        page = tuple(self._items[i] for i in self._order[start:start + self.page_size])
        if start == 0:
            page = (_ROOT_CHANGE,) + page
        end = start + self.page_size >= len(self._order)
        return DriveChanges(page, _DELTA if end else f"{_FULL}{start + self.page_size}", end, True,
                            DRIVE_ID, ROOT_ID)

    async def get_change(self, user_id: str, item_id: str) -> DriveChange:
        if item_id == ROOT_ID:
            return DriveChange(item_id=ROOT_ID, name="root", is_folder=True)
        return self._items.get(item_id) or DriveChange(item_id=item_id, deleted=True)

    async def list_children_changes(self, user_id: str, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        if folder_id != ROOT_ID and folder_id not in self._items:
            raise DriveItemNotFoundError(folder_id)
        kids = sorted((c for c in self._items.values() if c.parent_id == folder_id), key=lambda c: c.name)
        start = int(cursor[len(_CHILDREN):]) if cursor else 0
        nxt = f"{_CHILDREN}{start + limit}" if start + limit < len(kids) else None
        return DriveChildrenPage(tuple(kids[start:start + limit]), nxt)

    async def connection(self, user_id: str) -> DriveConnection:
        return DriveConnection(connected=True, needs_reconnect=False, drive_id=DRIVE_ID)

    async def flag_reconnect(self, user_id: str) -> bool:
        return False

    # -- Step 1 surface: not used by indexing or search -----------------------------------------

    @property
    def display_name(self) -> str:
        return "Eval fixture"

    async def is_connected(self, user_id: str) -> bool:
        return True

    async def disconnect(self, user_id: str) -> None:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def get_root(self, user_id: str) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def get_item(self, user_id: str, item_id: str) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def list_children(self, user_id: str, folder_id: str) -> List[DriveItem]:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def search(self, user_id: str, query: str, limit: int) -> List[DriveItem]:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def download(self, user_id: str, item_id: str) -> bytes:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def upload(self, user_id: str, parent_id: str, filename: str, data: bytes,
                     content_type: str) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def replace_content(self, user_id: str, item_id: str, data: bytes, content_type: str) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def move(self, user_id: str, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def create_folder(self, user_id: str, parent_id: str, name: str) -> DriveItem:
        raise NotImplementedError("the eval drive serves indexing reads only")

    async def delete(self, user_id: str, item_id: str) -> None:
        raise NotImplementedError("the eval drive serves indexing reads only")

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5): eval file bytes come later --

    async def download_to_file(self, user_id: str, item_id: str, dest_path: str, max_bytes: int) -> int:
        raise NotImplementedError("eval file bytes — plan part E")

    async def get_thumbnail(self, user_id: str, item_id: str, size: str) -> Optional[bytes]:
        raise NotImplementedError("eval file bytes — plan part E")
