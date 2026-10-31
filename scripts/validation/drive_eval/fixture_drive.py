"""
FixtureDrive — a UserDrivePort over the eval manifest (tests/fixtures/drive_eval/tree.json).

Read-only (USER_DRIVE_INDEXING_RFC §4). The first `get_changes` call pages a full
enumeration in parent-first order; the cursor it ends with yields delta rounds with no real change from
then on. As in S0a, every round carries the App Folder root itself first. Ids are a hash of the path, so
they are stable across runs and recorded bars always mean the same items. The manifest's facets keep Graph's raw shape
(S0e: `photo` may be `{}`, audio has no facet); they are read through the OneDrive adapter's own parsers into the
typed `DriveMediaFacts` and `ContentIdentity` (MULTI_DRIVE_RFC §7.2), and identities are the adapter's
(`microsoft_onedrive:{drive}:{item}`), so a note bound on the fixture binds as on the real drive. Folder listings are paged like Graph's (`limit`, opaque cursor).

File bytes (Slice 2+4, §8.1, §24.5): an item's `content` names a file under the manifest's folder
(tests/fixtures/drive_eval/files/, made by make_fixtures.py) and its size becomes that file's length; `thumbnail` names
the provider rendition served for every size (an image item's own bytes when it has none). A generate entry's
`content_template` names a template in fixture_texts, rendered per item (a PDF for `.pdf` names, UTF-8 text
otherwise), so the bulk folders are never committed. An item without bytes downloads as an empty file. The Step 1
reads (`get_root`, `get_item`, `list_children`, `download`) serve the Smart dry run (G3).

An item with `"inaccessible": true` (USER_DRIVE_INDEXING_RFC §26.2, D33) is listed by delta as any other but answers
"not found" to every id-addressed call (`get_change`, `get_item`, `download`, `download_to_file`, `get_thumbnail`),
as Graph does for an item whose own permission list lacks the app's entry; it still has bytes in the manifest, so the
fixture check "every file the indexer would read has bytes" holds, and the drive never serves them.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Tuple

from src.adapters.microsoft.onedrive_adapter import (
    content_identity_from_graph, media_facts_from_graph, onedrive_drive_identity, onedrive_item_identity,
)
from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_handle import DriveHandle
from src.domain.drive_index import DriveChange, DriveChanges, DriveChildrenPage
from src.domain.drive_media_source import MediaSource
from src.domain.user_drive import (
    DriveFileTooLarge, DriveItem, DriveItemNotFoundError, DriveNameConflictError,
    DriveResyncRequired,
)
from src.ports.user_drive_port import UserDrivePort

from . import fixture_texts, fixture_texts_afc
from .mini_pdf import text_pdf

ROOT_ID = "fx-root"
DRIVE_ID = "fx-drive"
DEFAULT_FS_MODIFIED = "2026-01-15T09:00:00Z"   # generated entries without their own date
AREA_PATH = "/Apps/Alek-bot"                   # synthetic: the area root's absolute path (child_by_name, area_path)
_FULL = "fx:full:"
_DELTA = "fx:delta"
_CHILDREN = "fx:children:"
_ROOT_CHANGE = DriveChange(item_id=ROOT_ID, name="root", is_folder=True,
                           identity=onedrive_item_identity(DRIVE_ID, ROOT_ID))   # S0a: in every round, no `root` facet
_RAW_FACETS = ("photo", "location", "image", "video", "audio", "remoteItem", "package", "malware")


# content_template name -> the template text; `{n}` is the generated item's number
TEMPLATES: Mapping[str, str] = {
    "vault_note": fixture_texts.VAULT_NOTE_TEMPLATE,
    "invoice_2024": fixture_texts.INVOICE_2024_TEMPLATE,
    "g3_injected_note": fixture_texts.G3_INJECTED_NOTE_TEMPLATE,   # tree_g3.json only (final review F3-2)
    # tree_g3.json only (DRIVE_SPECIALIST_RFC §15): the lead scenarios of the drive specialist eval
    "lead_locksmith_note": fixture_texts.LEAD_LOCKSMITH_NOTE_TEMPLATE,
    "lead_factura_fk": fixture_texts.LEAD_FACTURA_FK_TEMPLATE,
    "lead_garage_rental": fixture_texts.LEAD_GARAGE_RENTAL_TEMPLATE,
    "lead_normas": fixture_texts.LEAD_NORMAS_TEMPLATE,
    # tree_g3.json only (plan S2.6): the invented Acme R&D documents of the "answer from content" eval
    **fixture_texts_afc.TEMPLATES,
}


def render_template(name: str, n: int, file_name: str) -> bytes:
    text = TEMPLATES[name].format(n=n)
    if name in fixture_texts_afc.SCAN_PDF_TEMPLATES:
        return fixture_texts_afc.scan_pdf(text.splitlines())
    if file_name.lower().endswith(".docx"):
        return fixture_texts_afc.docx_from_lines(text.splitlines())
    if file_name.lower().endswith(".pdf"):
        return text_pdf(text.splitlines())
    return text.encode("utf-8")


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


def _graph_raw(entry: Mapping[str, Any], folder: bool, size: int) -> Dict[str, Any]:
    """The manifest entry as the Graph driveItem the adapter's parsers read (facets raw, `file.hashes` as in Graph)."""
    facets = entry.get("facets") or {}
    raw: Dict[str, Any] = {key: copy.deepcopy(facets[key]) for key in _RAW_FACETS if key in facets}
    raw["size"] = size
    if folder:
        raw["folder"] = {}
    else:
        hashes = (facets.get("file") or {}).get("hashes")
        raw["file"] = {"hashes": dict(hashes)} if hashes else {}
    return raw


class FixtureDrive(UserDrivePort):
    def __init__(self, manifest: Mapping[str, Any], page_size: int = 200, base_dir: Optional[Path] = None) -> None:
        self.page_size = page_size
        self._base_dir = base_dir
        self._items: Dict[str, DriveChange] = {}
        self._paths: Dict[str, str] = {}                 # item_id -> path
        self._content: Dict[str, bytes] = {}             # item_id -> file bytes
        self._thumbnails: Dict[str, bytes] = {}          # item_id -> provider rendition
        self._generated: Dict[str, Tuple[str, int]] = {}  # item_id -> (content_template, n) of a bulk member
        self._raw: Dict[str, Dict[str, Any]] = {}        # item_id -> the Graph-shaped raw item the parsers read
        self._closed: set = set()                        # §26.2: listed by delta, 404 on every id-addressed call
        self.downloads: List[str] = []
        self._version = 0                                # upload counter: a new cTag per write
        self.thumbnail_calls: List[Tuple[str, int]] = []
        for entry in manifest.get("items", []):
            self._add(entry["path"], bool(entry.get("folder")), entry)
        for gen in manifest.get("generate", []):
            for n in range(int(gen["count"])):
                name = gen["pattern"].format(n=n)
                template = gen.get("content_template")
                self._add(f"{gen['folder']}/{name}", False, {
                    "mime": gen.get("mime", ""), "size": gen.get("size", 0),
                    "fs_modified": gen.get("fs_modified", DEFAULT_FS_MODIFIED),
                    "facets": _format_strings(gen.get("facets_template") or {}, n),
                }, render=(lambda t=template, k=n, f=name: render_template(t, k, f)) if template else None)
                if template:
                    self._generated[item_id_for(f"{gen['folder']}/{name}")] = (template, n)
        counts: Dict[str, int] = {}
        for change in self._items.values():
            counts[change.parent_id] = counts.get(change.parent_id, 0) + 1
        for item_id, change in list(self._items.items()):
            if change.is_folder:
                self._items[item_id] = replace(change, child_count=counts.get(item_id, 0))
        self._order = sorted(self._items, key=lambda i: self._paths[i].split("/"))

    @classmethod
    def from_file(cls, path: Path, page_size: int = 200) -> "FixtureDrive":
        return cls(json.loads(path.read_text(encoding="utf-8")), page_size, base_dir=path.parent)

    def _read(self, relative: str) -> bytes:
        if self._base_dir is None:
            raise ValueError(f"{relative}: a manifest with file bytes needs base_dir (FixtureDrive.from_file)")
        return (self._base_dir / relative).read_bytes()

    # -- manifest -> observations ----------------------------------------------------------------

    def _ensure_folder(self, path: str) -> str:
        if not path:
            return ROOT_ID
        item_id = item_id_for(path)
        if item_id not in self._items:
            self._add(path, True, {})
        return item_id

    def _add(self, path: str, folder: bool, entry: Mapping[str, Any],
             render: Optional[Callable[[], bytes]] = None) -> None:
        path = path.strip("/")
        parent_path, _, name = path.rpartition("/")
        parent_id = self._ensure_folder(parent_path)
        item_id = item_id_for(path)
        modified = _parse_dt(entry.get("fs_modified") or DEFAULT_FS_MODIFIED)
        size = int(entry.get("size", 0))
        if not folder and entry.get("content"):
            self._content[item_id] = self._read(entry["content"])
        elif not folder and render is not None:
            self._content[item_id] = render()
        if item_id in self._content:
            size = len(self._content[item_id])
        if not folder and entry.get("thumbnail"):
            self._thumbnails[item_id] = self._read(entry["thumbnail"])
        self._paths[item_id] = path
        if entry.get("inaccessible"):
            self._closed.add(item_id)
        self._raw[item_id] = _graph_raw(entry, folder, 0 if folder else size)
        self._items[item_id] = self._change(item_id, parent_id, name, folder, size, str(entry.get("mime", "")),
                                            "" if folder else f"ctag-{item_id}", modified, modified)

    def _change(self, item_id: str, parent_id: str, name: str, folder: bool, size: int, mime: str, version: str,
                modified: datetime, created: datetime, child_count: int = 0) -> DriveChange:
        raw = self._raw.get(item_id) or {}
        raw = {**raw, "size": 0 if folder else size}
        return DriveChange(
            item_id=item_id, parent_id=parent_id, name=name, is_folder=folder, size_bytes=0 if folder else size,
            mime_type="" if folder else mime, content_version=version, modified_at=modified, fs_modified_at=modified,
            created_at=created, child_count=child_count,
            identity=onedrive_item_identity(DRIVE_ID, item_id), content_identity=content_identity_from_graph(raw),
            media=media_facts_from_graph(raw, drive_id=DRIVE_ID))

    # -- lookups for the runner ------------------------------------------------------------------

    def path_of(self, item_id: str) -> Optional[str]:
        return self._paths.get(item_id)

    def generated_member(self, item_id: str) -> Optional[Tuple[str, int]]:
        """(content_template, n) of a file a generate entry rendered from a template, else None: the Search v2
        replay gives these bulk members their template's hand-written profile (enrichment_replay.py)."""
        return self._generated.get(item_id)

    def set_content(self, path: str, data: bytes) -> None:
        """Serve `data` as the bytes of the file at `path` (the scale corpora keep their bodies in memory)."""
        item_id = item_id_for(path.strip("/"))
        self._content[item_id] = data
        self._items[item_id] = replace(self._items[item_id], size_bytes=len(data))
        self._raw[item_id] = {**self._raw.get(item_id, {}), "size": len(data)}

    def is_inaccessible(self, path: str) -> bool:
        return item_id_for(path.strip("/")) in self._closed

    def _check(self, item_id: str) -> None:
        if item_id in self._closed:
            raise DriveItemNotFoundError(item_id)

    def paths(self) -> List[str]:
        return [self._paths[i] for i in self._order]

    def has_content(self, path: str) -> bool:
        return item_id_for(path) in self._content

    def content_sha256(self) -> str:
        """One hash over every byte the drive serves (files, renditions, rendered templates), keyed by path: the bars
        pin it, so a regenerated fixture or an edited template gives no verdict until the bars are recorded again."""
        digest = hashlib.sha256()
        for item_id in self._order:
            for label, store in (("c", self._content), ("t", self._thumbnails)):
                if item_id in store:
                    digest.update(f"{label}:{self._paths[item_id]}:".encode("utf-8"))
                    digest.update(hashlib.sha256(store[item_id]).digest())
        return digest.hexdigest()

    def _item(self, item_id: str) -> DriveItem:
        drive_identity = onedrive_drive_identity(DRIVE_ID)
        if item_id == ROOT_ID:
            return DriveItem(item_id=ROOT_ID, name="root", path="", is_folder=True,
                             identity=onedrive_item_identity(DRIVE_ID, ROOT_ID), drive_identity=drive_identity)
        change = self._items.get(item_id)
        if change is None:
            raise DriveItemNotFoundError(item_id)
        return DriveItem(item_id=item_id, name=change.name, path=self._paths[item_id], is_folder=change.is_folder,
                         size_bytes=change.size_bytes, mime_type=change.mime_type, modified_at=change.modified_at,
                         child_count=change.child_count or 0, identity=change.identity, drive_identity=drive_identity,
                         content_version="" if change.is_folder else change.content_version,
                         content_identity=change.content_identity, media=change.media)

    # -- indexing reads (USER_DRIVE_INDEXING_RFC §4) -------------------------------------------

    async def get_changes(self, handle: DriveHandle, cursor: Optional[str]) -> DriveChanges:
        if cursor == _DELTA:
            return DriveChanges((_ROOT_CHANGE,), _DELTA, True, False, ROOT_ID, onedrive_drive_identity(DRIVE_ID))
        if cursor is not None and not cursor.startswith(_FULL):
            raise DriveResyncRequired(f"{_FULL}0")
        start = int(cursor[len(_FULL):]) if cursor else 0
        page = tuple(self._items[i] for i in self._order[start:start + self.page_size])
        if start == 0:
            page = (_ROOT_CHANGE,) + page
        end = start + self.page_size >= len(self._order)
        return DriveChanges(page, _DELTA if end else f"{_FULL}{start + self.page_size}", end, True,
                            ROOT_ID, onedrive_drive_identity(DRIVE_ID))

    async def get_change(self, handle: DriveHandle, item_id: str) -> DriveChange:
        if item_id == ROOT_ID:
            return _ROOT_CHANGE
        self._check(item_id)
        found = self._items.get(item_id)
        if found is None:   # the port contract (USER_DRIVE_INDEXING_RFC §26.2, Δ48): "not found" raises, never a deletion
            raise DriveItemNotFoundError(item_id)
        return found

    async def list_children_changes(self, handle: DriveHandle, folder_id: str, limit: int,
                                    cursor: Optional[str] = None) -> DriveChildrenPage:
        if folder_id != ROOT_ID and folder_id not in self._items:
            raise DriveItemNotFoundError(folder_id)
        kids = sorted((c for c in self._items.values() if c.parent_id == folder_id), key=lambda c: c.name)
        start = int(cursor[len(_CHILDREN):]) if cursor else 0
        nxt = f"{_CHILDREN}{start + limit}" if start + limit < len(kids) else None
        return DriveChildrenPage(tuple(kids[start:start + limit]), nxt)

    # -- Step 1 surface: reads for the Smart dry run (G3); writes are refused ---------------------

    async def get_root(self, handle: DriveHandle) -> DriveItem:
        return self._item(ROOT_ID)

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        self._check(item_id)
        return self._item(item_id)

    async def list_children(self, handle: DriveHandle, folder_id: str) -> List[DriveItem]:
        if folder_id != ROOT_ID and folder_id not in self._items:
            raise DriveItemNotFoundError(folder_id)
        kids = sorted((c for c in self._items.values() if c.parent_id == folder_id), key=lambda c: c.name)
        return [self._item(c.item_id) for c in kids]

    async def search(self, handle: DriveHandle, query: str, limit: int) -> List[DriveItem]:
        raise NotImplementedError("the eval drive is read-only")

    async def download(self, handle: DriveHandle, item_id: str) -> bytes:
        self._check(item_id)
        if item_id not in self._items:
            raise DriveItemNotFoundError(item_id)
        return self._content.get(item_id, b"")

    async def upload(self, handle: DriveHandle, parent_id: str, filename: str, data: bytes, content_type: str,
                     conflict: Literal["rename", "replace", "fail"] = "rename") -> DriveItem:
        """The one write the fixture takes (the mirror's tests): `rename` suffixes a taken name ("a 1.md"),
        `replace` overwrites the file keeping its id, `fail` raises DriveNameConflictError."""
        if parent_id != ROOT_ID and (parent_id not in self._items or not self._items[parent_id].is_folder):
            raise DriveItemNotFoundError(parent_id)
        parent_path = "" if parent_id == ROOT_ID else self._paths[parent_id]

        def at(name: str) -> str:
            return f"{parent_path}/{name}" if parent_path else name

        name = filename
        taken = item_id_for(at(name)) in self._items
        if taken and conflict == "fail":
            raise DriveNameConflictError(f"Drive name taken: {filename}")
        if taken and conflict == "rename":
            stem, dot, ext = filename.rpartition(".") if "." in filename else (filename, "", "")
            n = 1
            while item_id_for(at(f"{stem} {n}{dot}{ext}")) in self._items:
                n += 1
            name = f"{stem} {n}{dot}{ext}"
        path = at(name)
        item_id = item_id_for(path)
        self._version += 1
        existing = self._items.get(item_id)
        now = datetime.now(timezone.utc)
        self._content[item_id] = data
        self._paths[item_id] = path
        self._raw[item_id] = {"size": len(data), "file": {"hashes": {"quickXorHash": hashlib.sha1(data).hexdigest()[:27]}}}
        self._items[item_id] = self._change(item_id, parent_id, name, False, len(data), content_type,
                                            f"ctag-{item_id}-{self._version}", now,
                                            existing.created_at if existing and existing.created_at else now)
        self._order = sorted(self._items, key=lambda i: self._paths[i].split("/"))
        return self._item(item_id)

    async def child_by_name(self, handle: DriveHandle, parent_id: str, name: str) -> Optional[DriveChange]:
        if parent_id != ROOT_ID and parent_id not in self._items:
            raise DriveItemNotFoundError(parent_id)
        for change in self._items.values():
            if change.parent_id == parent_id and change.name == name and change.item_id not in self._closed:
                return change
        return None

    async def area_path(self, handle: DriveHandle) -> str:
        return AREA_PATH

    async def replace_content(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> DriveItem:
        raise NotImplementedError("the eval drive is read-only")

    async def move(self, handle: DriveHandle, item_id: str, new_parent_id: Optional[str] = None,
                   new_name: Optional[str] = None) -> DriveItem:
        raise NotImplementedError("the eval drive is read-only")

    async def create_folder(self, handle: DriveHandle, parent_id: str, name: str) -> DriveItem:
        raise NotImplementedError("the eval drive is read-only")

    async def delete(self, handle: DriveHandle, item_id: str) -> None:
        raise NotImplementedError("the eval drive is read-only")

    # -- content for the index (USER_DRIVE_INDEXING_RFC §8.1, §24.5) -----------------------------------

    async def download_to_file(self, handle: DriveHandle, item_id: str, dest_path: str, max_bytes: int) -> int:
        """As a cut stream: what fits under `max_bytes` is written, then DriveFileTooLarge (§5.4)."""
        self.downloads.append(item_id)
        self._check(item_id)
        if item_id not in self._items or self._items[item_id].is_folder:
            raise DriveItemNotFoundError(item_id)
        data = self._content.get(item_id, b"")
        with open(dest_path, "wb") as fh:
            fh.write(data[:max_bytes])
        if len(data) > max_bytes:
            raise DriveFileTooLarge(max_bytes)
        return len(data)

    async def media_source(self, handle: DriveHandle, item_id: str) -> MediaSource:
        """A header-free source for a file the fixture serves (the eval never fetches it: there is no network)."""
        self._check(item_id)
        change = self._items.get(item_id)
        if change is None or change.is_folder:
            raise DriveItemNotFoundError(item_id)
        return MediaSource(url=f"fixture://{DRIVE_ID}/{item_id}")

    async def capabilities(self, handle: DriveHandle) -> DriveCapabilities:
        return DriveCapabilities(max_path_chars=400)

    async def forget(self, handle: DriveHandle) -> None:
        """The fixture holds no per-connection cache."""

    async def get_thumbnail(self, handle: DriveHandle, item_id: str, max_edge_px: int) -> Optional[bytes]:
        """The same rendition for every size: the fixture's pictures are already small (S0f, S0g)."""
        if max_edge_px <= 0:
            raise ValueError("a thumbnail edge is positive")
        self.thumbnail_calls.append((item_id, max_edge_px))
        self._check(item_id)
        change = self._items.get(item_id)
        if change is None:
            raise DriveItemNotFoundError(item_id)
        if item_id in self._thumbnails:
            return self._thumbnails[item_id]
        if change.mime_type.startswith("image/") and item_id in self._content:
            return self._content[item_id]
        return None
