"""
InMemoryDriveMirrorRepository — DriveMirrorRepository in process memory: the second implementation of the port, the
reference the Firestore adapter is held against (tests/unit/adapters/test_drive_mirror_repo_contract.py runs one case list
on both). Ordering matches the Firestore adapter: by document key unless the port says otherwise.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from ..domain.drive_mirror import (
    KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ARCHIVED, MIRROR_DETACHED, DeletionNotice, MirrorDoc, MirrorRoot,
    ledger_key,
)
from ..domain.drive_mirror_plan import mark_held, marked, unmarked
from ..ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY, DriveMirrorRepository

_KEYS_MAX = 30   # Firestore's value limit per array-contains-any, kept so the two adapters refuse alike (truncated)
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


class InMemoryDriveMirrorRepository(DriveMirrorRepository):
    def __init__(self) -> None:
        self.root_docs: Dict[Tuple[str, str], MirrorRoot] = {}
        self.docs: Dict[str, MirrorDoc] = {}

    def _drive(self, user_id: str, drive_identity: str) -> List[MirrorDoc]:
        return sorted((d for d in self.docs.values() if d.user_id == user_id and d.drive_identity == drive_identity),
                      key=lambda d: d.key)

    def _user(self, user_id: str) -> List[MirrorDoc]:
        return sorted((d for d in self.docs.values() if d.user_id == user_id), key=lambda d: d.key)

    # -- roots ---------------------------------------------------------------------------------

    async def roots(self, user_id: str) -> List[MirrorRoot]:
        return sorted((r for (u, _), r in self.root_docs.items() if u == user_id), key=lambda r: r.drive_identity)

    async def get_root(self, user_id: str, drive_identity: str) -> Optional[MirrorRoot]:
        return self.root_docs.get((user_id, drive_identity))

    async def save_root(self, root: MirrorRoot) -> None:
        root.key   # ValueError for an empty drive identity, as the Firestore adapter
        self.root_docs[(root.user_id, root.drive_identity)] = root

    async def mark_root_gone(self, user_id: str, root_item_id: str, at: datetime) -> bool:
        found = False
        for key, root in list(self.root_docs.items()):
            if key[0] == user_id and root.root_item_id == root_item_id:
                found = True
                if root.root_gone_at is None:
                    self.root_docs[key] = replace(root, root_gone_at=at)
        return found

    async def clear_root_gone(self, user_id: str, root_item_id: str) -> bool:
        found = False
        for key, root in list(self.root_docs.items()):
            if key[0] == user_id and root.root_item_id == root_item_id:
                found = True
                self.root_docs[key] = replace(root, root_gone_at=None)
        return found

    # -- ledger documents -------------------------------------------------------------------------

    async def get_docs(self, user_id: str, keys: Sequence[str]) -> Dict[str, MirrorDoc]:
        return {k: self.docs[k] for k in dict.fromkeys(keys) if k in self.docs and self.docs[k].user_id == user_id}

    async def save_doc(self, doc: MirrorDoc) -> None:
        self.docs[doc.key] = doc

    async def delete_doc(self, user_id: str, key: str) -> None:
        if key in self.docs and self.docs[key].user_id == user_id:
            del self.docs[key]

    async def by_mirror_item(self, user_id: str, mirror_item_id: str) -> Optional[MirrorDoc]:
        found = [d for d in self._user(user_id) if d.mirror_item_id == mirror_item_id]
        return found[0] if found else None

    # -- marks --------------------------------------------------------------------------------------

    async def mark_gone(self, user_id: str, marks: Sequence[DeletionNotice], *, drive_identity: str) -> int:
        if not drive_identity:
            raise ValueError("mark_gone needs the mirror's drive identity")
        by_key: Dict[str, List[DeletionNotice]] = {}
        for notice in marks:
            if not notice.source_identity:
                continue
            for kind in (KIND_FILE, KIND_FOLDER):
                by_key.setdefault(ledger_key(user_id, kind, drive_identity, notice.source_identity), []).append(notice)
        updated = 0
        for key, notices in by_key.items():
            doc = self.docs.get(key)
            if doc is None or doc.user_id != user_id or doc.drive_identity != drive_identity:
                continue
            for notice in notices:
                doc = marked(doc, notice)
            self.docs[key] = doc
            updated += 1
        return updated

    async def clear_mark(self, user_id: str, key: str) -> None:
        doc = self.docs.get(key)
        if doc is not None and doc.user_id == user_id:
            self.docs[key] = unmarked(doc)

    async def hold_mark(self, user_id: str, key: str, until: datetime) -> None:
        doc = self.docs.get(key)
        if doc is not None and doc.user_id == user_id:
            self.docs[key] = mark_held(doc, until)

    async def ready_marks(self, user_id: str, drive_identity: str, now: datetime, limit: int) -> List[MirrorDoc]:
        ripe = [d for d in self._drive(user_id, drive_identity)
                if d.gone_pending and d.mark_ready_at is not None and d.mark_ready_at <= now]
        return sorted(ripe, key=lambda d: (d.mark_ready_at, d.key))[:limit]

    # -- the step's reads ------------------------------------------------------------------------------

    async def children(self, user_id: str, drive_identity: str, mirror_parent_id: str, limit: int) -> List[MirrorDoc]:
        return [d for d in self._drive(user_id, drive_identity) if d.mirror_parent_id == mirror_parent_id][:limit]

    async def folder_docs(self, user_id: str, drive_identity: str, after_key: Optional[str],
                          limit: int) -> List[MirrorDoc]:
        return [d for d in self._drive(user_id, drive_identity)
                if d.kind in (KIND_FOLDER, KIND_ARCHIVE) and (after_key is None or d.key > after_key)][:limit]

    async def scan_page(self, user_id: str, drive_identity: str, after_key: Optional[str], limit: int) -> List[MirrorDoc]:
        return [d for d in self._drive(user_id, drive_identity)
                if d.kind == KIND_FILE and (after_key is None or d.key > after_key)][:limit]

    async def detached_due(self, user_id: str, drive_identity: str, before: datetime, limit: int) -> List[MirrorDoc]:
        detached = [d for d in self._drive(user_id, drive_identity) if d.state == MIRROR_DETACHED]
        never = [d for d in detached if d.checked_at is None]
        old = sorted((d for d in detached if d.checked_at is not None and d.checked_at < before),
                     key=lambda d: (d.checked_at, d.key))
        return (never + old)[:limit]

    # -- the deleted-files search -------------------------------------------------------------------------

    async def archived_newest(self, user_id: str, limit: int) -> List[MirrorDoc]:
        archived = [d for d in self._user(user_id) if d.state == MIRROR_ARCHIVED]
        # Firestore orders a null below every timestamp; ties break on the key, descending with the order
        return sorted(archived, key=lambda d: (d.archived_at or _OLDEST, d.key), reverse=True)[:limit]

    async def archived_by_keys(self, user_id: str, family: str, keys: Sequence[str], limit: int) -> List[MirrorDoc]:
        if family not in (ARCHIVE_FAMILY, NAME_FAMILY):
            raise ValueError(f"archived_by_keys: unknown family {family!r}")
        wanted = set(list(dict.fromkeys(keys))[:_KEYS_MAX])
        field = "archive_keys" if family == ARCHIVE_FAMILY else "name_keys"
        return [d for d in self._user(user_id)
                if d.state == MIRROR_ARCHIVED and wanted.intersection(getattr(d, field))][:limit]

    # -- pins -------------------------------------------------------------------------------------------------

    @staticmethod
    def _pins(doc: MirrorDoc) -> bool:
        return doc.state in (MIRROR_ARCHIVED, MIRROR_DETACHED) or doc.gone_pending

    async def pinned(self, user_id: str, content_key: str) -> bool:
        return bool(content_key) and any(d.content_key == content_key and self._pins(d) for d in self._user(user_id))

    async def pinned_keys(self, user_id: str) -> FrozenSet[str]:
        return frozenset(d.content_key for d in self._user(user_id) if d.content_key and self._pins(d))
