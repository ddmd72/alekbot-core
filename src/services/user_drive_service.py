"""
UserDriveService — rules over UserDrivePort (docs/10_rfcs/USER_DRIVE_RFC.md §4.7–§4.11).

Deterministic, no LLM: destination folders resolved segment by segment (case-insensitive,
missing segments created, a create race resolved by re-listing); a save never overwrites
and never duplicates identical content; append keeps everything that was there; the area
root is never deleted or moved; a folder's file count is taken before it is deleted.

After every successful mutation it nudges the drive index (USER_DRIVE_INDEXING_RFC §7.1 step 1, §13):
one `drive_sync` task per mutation carrying every item id the mutation touched (RFC Δ3), so the
indexer applies the bot's own write before delta catches up. A nudge is best-effort: it runs inside
the agent's shielded mutation, after the provider call succeeded, and its failure is logged, never
raised — the mutation already happened.

Multi-drive (MULTI_DRIVE_RFC §6, E6): every drive method takes the `DriveHandle` of the connection it acts on, where it
took `user_id`; the nudge carries the connection (`{user_id, connection_id}`). Callers obtain the handle through this
service's connection reads, which delegate to the injected DriveConnectionService: `primary` for a path (stage 1),
`resolve_ref` for a ref (§9). Root protection, the mirror guard and the duplicate check are per connection.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple, Union

from ..domain.user_drive import (
    DEFAULT_INBOX_FOLDER,
    MAX_DRIVE_APPEND_FILE_BYTES,
    MAX_DRIVE_DOWNLOAD_BYTES,
    MOVE_NEEDS_TARGET,
    DeleteOutcome,
    DriveItem,
    DriveItemNotFoundError,
    DriveMirrorWriteRefused,
    DriveNameConflictError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRefRefused,
    DriveRootProtectedError,
    FolderResolution,
    ListOutcome,
    MoveOutcome,
    SaveOutcome,
    UpdateOutcome,
    format_size,
    is_text_file,
    join_drive_path,
    match_child_folder,
    name_key,
    sanitize_drive_filename,
    split_folder_path,
)
from ..domain.drive_connection import DriveConnection
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_handle import DriveHandle
from ..domain.drive_index import Nudge
from ..domain.drive_index_params import TICK_DEADLINE_S
from ..domain.drive_ref_refusal import DriveRefRefusal
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_task_key, scope_payload
from ..ports.drive_mirror_repository import DriveMirrorRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .drive_connection_service import DriveConnectionService

LIST_LIMIT = 100
SEARCH_LIMIT = 25
COUNT_CAP = 10_000


def _require_valid_name(name: str) -> None:
    """Refuse a name the provider would reject, before any provider call (it would surface as a raw 400)."""
    if sanitize_drive_filename(name) != name:
        raise DrivePathError(f"'{name}' is not a valid name: it contains characters a drive forbids "
                             "(\" * : < > ? / \\ |), or ends with a dot or space")


class UserDriveService:
    def __init__(self, drive: UserDrivePort, connections: "DriveConnectionService",
                 nudge_queue: Optional[TaskQueue] = None,
                 mirror_roots: Optional[DriveMirrorRepository] = None) -> None:
        """`drive` is the composition's router over every drive kind; `connections` the one owner of the user's drive
        connections (MULTI_DRIVE_RFC §4.4). `mirror_roots` is the transcript mirror's repository: with it, every
        mutation is checked against the bot's `_Transcripts` folder of the connection's drive (§27.9.8, the Step 1
        guard). Without it, or for a user with no root, nothing extra is read."""
        self._drive = drive
        self._connections = connections
        self._nudge_queue = nudge_queue
        self._mirror_roots = mirror_roots

    # -- which connection (MULTI_DRIVE_RFC §6, §9) -------------------------------------------------------------------

    async def status(self, user_id: str) -> DriveConnectionStatus:
        """The primary connection's status, as the gate reads it (the drive specialist's preflight, §5.3)."""
        return await self._connections.status(user_id)

    async def primary(self, user_id: str) -> DriveConnection:
        """The connection a path is addressed in (stage 1: the primary). DriveNotConnectedError when there is none."""
        connection = await self._connections.primary(user_id)
        if connection is None:
            raise DriveNotConnectedError("no primary drive connection")
        return connection

    async def resolve_ref(self, user_id: str, ref: str) -> ResolvedDriveRef:
        """The connection and item id of a `drive:<tag>:<item_id>` ref. DriveRefRefused with the code-built line for an
        untagged ref, a tag the user never had or a disconnected connection's (§9, D13); DrivePathError when `ref` holds
        no drive ref."""
        found: Optional[Union[ResolvedDriveRef, DriveRefRefusal]] = await self._connections.resolve_ref(user_id, ref)
        if found is None:
            raise DrivePathError(f"'{ref}' is not a drive: ref")
        if isinstance(found, DriveRefRefusal):
            raise DriveRefRefused(found.line)
        return found

    # -- operations on one connection --------------------------------------------------------------------------------

    async def resolve_folder(self, handle: DriveHandle, path: Optional[str], *, create: bool) -> FolderResolution:
        resolution, _ = await self._resolve(handle, path, create=create)
        return resolution

    async def _resolve(self, handle: DriveHandle, path: Optional[str], *,
                       create: bool) -> Tuple[FolderResolution, List[str]]:
        """The resolution plus the item ids of the folders this call created (for the nudge)."""
        segments = split_folder_path(path or "")
        for segment in segments:
            _require_valid_name(segment)
        current = await self._drive.get_root(handle)
        created: List[str] = []
        created_ids: List[str] = []
        for segment in segments:
            match = match_child_folder(await self._drive.list_children(handle, current.item_id), segment)
            if match is None:
                if not create:
                    raise DrivePathError(f"No folder '{join_drive_path(current.path, segment)}' on the drive")
                try:
                    match = await self._drive.create_folder(handle, current.item_id, segment)
                    created.append(match.path or join_drive_path(current.path, segment))
                    created_ids.append(match.item_id)
                except DriveNameConflictError as exc:
                    # A parallel save created it first (§4.7): use the winner.
                    match = match_child_folder(await self._drive.list_children(handle, current.item_id), segment)
                    if match is None:
                        # The name is taken, but not by a folder (a file of that name sits there).
                        taken = join_drive_path(current.path, segment)
                        logger.warning(f"Drive folder '{taken}' cannot be created: name taken by a non-folder")
                        raise DrivePathError(f"'{taken}' already exists as a file, so it cannot be used as "
                                             f"a folder") from exc
            current = match
        return FolderResolution(folder=current, created=created), created_ids

    async def ensure_folder(self, handle: DriveHandle, folder: str) -> FolderResolution:
        if not split_folder_path(folder):
            raise DrivePathError("A folder name is required")
        await self._refuse_mirror(handle, path=folder)
        resolution, created_ids = await self._resolve(handle, folder, create=True)
        await self._nudge(handle, created_ids)
        return resolution

    async def save(self, handle: DriveHandle, data: bytes, filename: str, content_type: str,
                   folder: Optional[str]) -> SaveOutcome:
        target = folder if folder and folder.strip() else DEFAULT_INBOX_FOLDER
        await self._refuse_mirror(handle, path=target)   # before _resolve(create=True) creates anything
        resolution, created_ids = await self._resolve(handle, target, create=True)
        children = await self._drive.list_children(handle, resolution.folder.item_id)
        wanted = name_key(filename)
        same_name = next((c for c in children if not c.is_folder and name_key(c.name) == wanted), None)
        if (same_name is not None and same_name.size_bytes == len(data)
                and len(data) <= MAX_DRIVE_DOWNLOAD_BYTES
                and await self._drive.download(handle, same_name.item_id) == data):
            await self._nudge(handle, created_ids)
            return SaveOutcome(item=same_name, created=resolution.created, already_existed=True)
        item = await self._drive.upload(handle, resolution.folder.item_id, filename, data, content_type)
        await self._nudge(handle, created_ids + [item.item_id])
        return SaveOutcome(item=item, created=resolution.created, renamed=name_key(item.name) != wanted)

    async def list_folder(self, handle: DriveHandle, folder: Optional[str]) -> ListOutcome:
        resolution = await self.resolve_folder(handle, folder, create=False)
        children = await self._drive.list_children(handle, resolution.folder.item_id)
        ordered = sorted(children, key=lambda i: (not i.is_folder, i.name.casefold()))
        return ListOutcome(folder=resolution.folder, items=ordered[:LIST_LIMIT], truncated=len(ordered) > LIST_LIMIT)

    async def list_folder_by_id(self, handle: DriveHandle, folder_id: str) -> ListOutcome:
        """A folder given by its `drive:` ref (USER_DRIVE_INDEXING_RFC §26.2, Δ55: search returns folder refs). A file
        ref is refused; a folder the provider does not find raises DriveItemNotFoundError."""
        folder = await self._drive.get_item(handle, folder_id)
        if not folder.is_folder:
            raise DrivePathError(f"'{folder.path}' is a file, not a folder; open it instead.")
        children = await self._drive.list_children(handle, folder.item_id)
        ordered = sorted(children, key=lambda i: (not i.is_folder, i.name.casefold()))
        return ListOutcome(folder=folder, items=ordered[:LIST_LIMIT], truncated=len(ordered) > LIST_LIMIT)

    async def search(self, handle: DriveHandle, query: str) -> List[DriveItem]:
        return await self._drive.search(handle, query, SEARCH_LIMIT)

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        return await self._drive.get_item(handle, item_id)

    async def move(self, handle: DriveHandle, item_id: str, folder: Optional[str], new_name: Optional[str]) -> MoveOutcome:
        if not (folder and folder.strip()) and not (new_name and new_name.strip()):
            raise DrivePathError(MOVE_NEEDS_TARGET)
        new_name = new_name.strip() if new_name and new_name.strip() else None
        if new_name is not None:
            _require_valid_name(new_name)
        await self._refuse_root(handle, item_id)
        before = await self._drive.get_item(handle, item_id)
        await self._refuse_mirror(handle, item=before)
        created: List[str] = []
        parent_id: Optional[str] = None
        created_ids: List[str] = []
        if folder and folder.strip():
            await self._refuse_mirror(handle, path=folder)
            self._refuse_move_into_self(before, folder)
            resolution, created_ids = await self._resolve(handle, folder, create=True)
            parent_id, created = resolution.folder.item_id, resolution.created
        after = await self._drive.move(handle, item_id, new_parent_id=parent_id,
                                       new_name=new_name)
        await self._nudge(handle, created_ids + [after.item_id])
        return MoveOutcome(before=before, after=after, created=created)

    async def append_text(self, handle: DriveHandle, item_id: str, text: str) -> UpdateOutcome:
        item = await self._drive.get_item(handle, item_id)
        await self._refuse_mirror(handle, item=item)
        if item.is_folder or not is_text_file(item.name, item.mime_type):
            raise DrivePathError(f"'{item.path}' is not a text file; text can only be appended to text files")
        if item.size_bytes > MAX_DRIVE_APPEND_FILE_BYTES:
            raise DrivePathError(f"'{item.path}' is {format_size(item.size_bytes)}; appending is limited to "
                                 f"{format_size(MAX_DRIVE_APPEND_FILE_BYTES)} files")
        old = await self._drive.download(handle, item_id)
        try:
            old.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning(f"Drive append refused: '{item.path}' is not UTF-8 text")
            raise DrivePathError(f"'{item.path}' is not UTF-8 text") from exc
        separator = b"" if not old or old.endswith(b"\n") else b"\n"
        addition = text if text.endswith("\n") else f"{text}\n"
        updated = await self._drive.replace_content(handle, item_id, old + separator + addition.encode("utf-8"),
                                                     item.mime_type or "text/plain")
        await self._nudge(handle, [updated.item_id])
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="append")

    async def replace_with(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> UpdateOutcome:
        item = await self._drive.get_item(handle, item_id)
        await self._refuse_mirror(handle, item=item)
        if item.is_folder:
            raise DrivePathError(f"'{item.path}' is a folder; only a file's content can be replaced")
        updated = await self._drive.replace_content(handle, item_id, data, content_type)
        await self._nudge(handle, [updated.item_id])
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="replace")

    async def delete(self, handle: DriveHandle, item_id: str) -> DeleteOutcome:
        await self._refuse_root(handle, item_id)
        item = await self._drive.get_item(handle, item_id)
        await self._refuse_mirror(handle, item=item)
        count, capped = (await self._count_files(handle, item.item_id)) if item.is_folder else (1, False)
        await self._drive.delete(handle, item_id)
        await self._nudge(handle, [item.item_id])
        return DeleteOutcome(item=item, file_count=count, count_capped=capped)

    async def _nudge(self, handle: DriveHandle, item_ids: Sequence[str]) -> None:
        """Tell the indexer which items this mutation touched (§7.1 step 1, RFC Δ3). Never raises."""
        ids = tuple(dict.fromkeys(i for i in item_ids if i))
        if self._nudge_queue is None or not ids:
            return
        nudge = Nudge(mutation_id=uuid.uuid4().hex, item_ids=ids)
        try:
            await self._nudge_queue.enqueue_worker_task(
                DRIVE_SYNC_TASK, scope_payload(handle.scope, nudge), deadline_seconds=TICK_DEADLINE_S,
                dedup_key=nudge_task_key(handle.scope, nudge.mutation_id, 0))
        except Exception as exc:
            logger.warning(f"Drive index nudge failed for {handle.user_id[:8]} ({len(ids)} items): {exc}")

    async def _refuse_root(self, handle: DriveHandle, item_id: str) -> None:
        if item_id == (await self._drive.get_root(handle)).item_id:
            raise DriveRootProtectedError("The drive area itself cannot be deleted or moved")

    async def _refuse_mirror(self, handle: DriveHandle, *, item: Optional[DriveItem] = None,
                             path: Optional[str] = None) -> None:
        """The Step 1 guard (§27.9.8): refuse a write whose target is the bot's transcript folder (a current or a
        previous root), lies under it, or (for an `item`) contains it. `item` is a source or target item; `path` a
        requested folder path (a save, a created folder, a move's destination), where only "is or under" matters. Only
        the roots of the handle's drive count (MULTI_DRIVE_RFC §10: roots are keyed by drive identity, read from the
        area root; a root stored without one counts too, so the guard never narrows). One query for the roots, one root
        read and one read per root path, only for a user who has a root. Paths are compared with `name_key`
        (case-insensitive, NFC), as folder resolution matches them."""
        if self._mirror_roots is None:
            return
        roots = await self._mirror_roots.roots(handle.user_id)
        if not roots:
            return
        drive_identity = (await self._drive.get_root(handle)).drive_identity
        roots = [r for r in roots if not r.drive_identity or not drive_identity or r.drive_identity == drive_identity]
        root_ids = {rid for r in roots for rid in (r.root_item_id, *r.previous_root_ids) if rid}
        if not root_ids:
            return
        if item is not None and item.item_id in root_ids:
            raise DriveMirrorWriteRefused("mirror root")
        wanted: List[Tuple[Tuple[str, ...], bool]] = []
        if item is not None and item.path:
            wanted.append((tuple(name_key(s) for s in split_folder_path(item.path)), item.is_folder))
        if path is not None:
            wanted.append((tuple(name_key(s) for s in split_folder_path(path)), False))
        if not any(parts for parts, _ in wanted):
            return
        for rid in sorted(root_ids):
            try:
                root_item = await self._drive.get_item(handle, rid)
            except DriveItemNotFoundError:
                continue   # a root that is gone has no path to guard; its id is checked above
            own = tuple(name_key(s) for s in split_folder_path(root_item.path or ""))
            if not own:
                continue
            for parts, contains in wanted:
                if not parts:
                    continue
                if parts[:len(own)] == own or (contains and own[:len(parts)] == parts):
                    raise DriveMirrorWriteRefused("mirror folder")

    @staticmethod
    def _refuse_move_into_self(item: DriveItem, folder: str) -> None:
        """A folder cannot be moved into itself or its own subtree. Decided on the item's path and
        the requested segments (same `name_key` match as resolution) before any provider call, so
        the missing segments of such a destination are never created."""
        if not item.is_folder or not item.path:
            return
        own = [name_key(s) for s in item.path.split("/")]
        wanted = [name_key(s) for s in split_folder_path(folder)]
        if wanted[:len(own)] == own:
            raise DrivePathError(f"'{item.path}' cannot be moved into itself")

    async def _count_files(self, handle: DriveHandle, folder_id: str) -> Tuple[int, bool]:
        """Files in the subtree; Graph's childCount covers direct children only (§4.10)."""
        count, pending = 0, [folder_id]
        while pending:
            for child in await self._drive.list_children(handle, pending.pop()):
                if child.is_folder:
                    pending.append(child.item_id)
                else:
                    count += 1
                    if count >= COUNT_CAP:
                        return count, True
        return count, False
