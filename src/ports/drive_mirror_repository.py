"""
DriveMirrorRepository — storage of the transcript mirror's roots and ledger
(docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §27.9.2, §27.9.3, §27.9.11; Δ199, Δ205, Δ206, Δ217).

Two collections, one lifecycle: no purge deletes either (the ledger is the bot's record of the files it wrote on a drive).
User-scoped by design (MULTI_DRIVE_RFC §10, T-5): roots are keyed by the drive's identity (`mirror_root_doc_id`) and
ledger documents by the mirror's drive identity and the source item's identity (`ledger_key`), so the folder outlives a
connection and a reconnect finds it. Every query of the mirror step takes `drive_identity` and filters on it (Δ206); only
the deleted-files search (`archived_*`) and the pins (`pinned`, `pinned_keys`) span drives, on purpose (Δ217). A missing composite index or a backend error
leaves every method as `DriveIndexStoreUnavailable`, which the tick treats as systemic (§8.2).

Public interface:
    roots / get_root / save_root / mark_root_gone / clear_root_gone       (the root documents)
    get_docs / save_doc / delete_doc / by_mirror_item                      (ledger documents)
    mark_gone / clear_mark / hold_mark / ready_marks                       (the deletion marks)
    children / folder_docs / scan_page / detached_due                      (the step's drive-scoped reads)
    archived_newest / archived_by_keys                                     (the deleted-files search)
    pinned / pinned_keys                                                   (the store entries the ledger pins)
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, FrozenSet, List, Optional, Sequence

from src.domain.drive_mirror import DeletionNotice, MirrorDoc, MirrorRoot

ARCHIVE_FAMILY = "archive"   # `archived_by_keys`: match on `archive_keys` (the transcript's words)
NAME_FAMILY = "name"         # `archived_by_keys`: match on `name_keys` (the source name's words)


class DriveMirrorRepository(ABC):

    # -- roots (§27.9.2) ---------------------------------------------------------------------

    @abstractmethod
    async def roots(self, user_id: str) -> List[MirrorRoot]:
        """Every root document of the user, one per drive, by drive identity. A read error raises, never an empty
        list."""

    @abstractmethod
    async def get_root(self, user_id: str, drive_identity: str) -> Optional[MirrorRoot]:
        """The root document of that drive, or None."""

    @abstractmethod
    async def save_root(self, root: MirrorRoot) -> None:
        """Write the whole root document (keyed `mirror_root_doc_id(user_id, drive_identity)`); ValueError for an empty
        drive identity."""

    @abstractmethod
    async def mark_root_gone(self, user_id: str, root_item_id: str, at: datetime) -> bool:
        """Set `root_gone_at` on the user's root document whose `root_item_id` it is (a previous root triggers nothing);
        an existing mark keeps its earlier time. Never creates a document. True when a document was found."""

    @abstractmethod
    async def clear_root_gone(self, user_id: str, root_item_id: str) -> bool:
        """Clear `root_gone_at` on the root document whose `root_item_id` it is. True when a document was found."""

    # -- ledger documents (§27.9.3) --------------------------------------------------------------

    @abstractmethod
    async def get_docs(self, user_id: str, keys: Sequence[str]) -> Dict[str, MirrorDoc]:
        """The existing documents among `keys` (a missing key is absent from the result); other users' keys are ignored."""

    @abstractmethod
    async def save_doc(self, doc: MirrorDoc) -> None:
        """Write the whole document under `doc.key`."""

    @abstractmethod
    async def delete_doc(self, user_id: str, key: str) -> None:
        """Delete the document; missing is a no-op, and another user's key is never touched."""

    @abstractmethod
    async def by_mirror_item(self, user_id: str, mirror_item_id: str) -> Optional[MirrorDoc]:
        """The document whose `mirror_item_id` it is (opening an archived file), or None. Spans drives."""

    # -- deletion marks (§27.9.4) --------------------------------------------------------------

    @abstractmethod
    async def mark_gone(self, user_id: str, marks: Sequence[DeletionNotice], *, drive_identity: str) -> int:
        """Mark the ledger documents of the noticed source items on the mirror of `drive_identity` (a `file` and a
        `folder` document, `ledger_key(user, kind, drive_identity, notice.source_identity)`, are the candidates for each
        notice; a notice without a source identity names no document and is skipped). Updates **existing** documents
        only, never creates one. On a marked document the earliest `gone_at` stays, `mark_ready_at` only moves later and
        the why only upgrades (`drive_mirror_plan.marked`); a new mark sets `mark_ready_at = at + MIRROR_ARCHIVE_GRACE`.
        The keys carry the drive (§10), so a document of another drive is never marked. ValueError for an empty drive
        identity. Returns the number of documents updated."""

    @abstractmethod
    async def clear_mark(self, user_id: str, key: str) -> None:
        """Clear the deletion mark of the document; a missing document is a no-op."""

    @abstractmethod
    async def hold_mark(self, user_id: str, key: str, until: datetime) -> None:
        """Move the mark's `mark_ready_at` to `until` if that is later (never earlier); a missing document is a no-op."""

    @abstractmethod
    async def ready_marks(self, user_id: str, drive_identity: str, now: datetime, limit: int) -> List[MirrorDoc]:
        """Documents of the drive with `gone_pending` and `mark_ready_at <= now`, earliest first: a held (or unripe)
        mark is excluded by the query itself (Δ217)."""

    # -- the mirror step's drive-scoped reads (Δ206) -------------------------------------------

    @abstractmethod
    async def children(self, user_id: str, drive_identity: str, mirror_parent_id: str, limit: int) -> List[MirrorDoc]:
        """Documents of the drive whose `mirror_parent_id` it is, by key."""

    @abstractmethod
    async def folder_docs(self, user_id: str, drive_identity: str, after_key: Optional[str],
                          limit: int) -> List[MirrorDoc]:
        """The audit's walk: documents of the drive with `kind` in (`folder`, `archive`), by key, after `after_key`."""

    @abstractmethod
    async def scan_page(self, user_id: str, drive_identity: str, after_key: Optional[str], limit: int) -> List[MirrorDoc]:
        """The scan's walk: documents of the drive with `kind == "file"`, by key, after `after_key`."""

    @abstractmethod
    async def detached_due(self, user_id: str, drive_identity: str, before: datetime, limit: int) -> List[MirrorDoc]:
        """`detached` documents of the drive never checked or checked before `before`, least recently checked first."""

    # -- the deleted-files search (§27.9.11), across drives ------------------------------------

    @abstractmethod
    async def archived_newest(self, user_id: str, limit: int) -> List[MirrorDoc]:
        """`archived` documents of the user, newest `archived_at` first."""

    @abstractmethod
    async def archived_by_keys(self, user_id: str, family: str, keys: Sequence[str], limit: int) -> List[MirrorDoc]:
        """`archived` documents of the user whose `archive_keys` (`ARCHIVE_FAMILY`) or `name_keys` (`NAME_FAMILY`)
        hold any of `keys` (at most 30), by key. An unknown family raises ValueError."""

    # -- pins (§27.9.6, Δ186, Δ217): per user, on every drive ----------------------------------------

    @abstractmethod
    async def pinned(self, user_id: str, content_key: str) -> bool:
        """True when a document of the user in `archived` or `detached`, or with `gone_pending`, references the key.
        Two queries, never one OR filter. An empty key is never pinned."""

    @abstractmethod
    async def pinned_keys(self, user_id: str) -> FrozenSet[str]:
        """Every content key the user's pinning documents reference (the same two queries, paged by the adapter)."""
