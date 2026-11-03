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

Access policy (MULTI_DRIVE_RFC §11.3, slice 2b): `access` reads the connection fresh (E-13) with its capabilities and
root; `ancestry` walks an item's parent chain by `get_change` (E-3); `check_access` runs `drive_access_policy.decide`,
walking only when a rule needs a chain (the App Folder default walks none), and raises DriveAccessRefused with the
policy's line. `write_folder_path`, `picker_folders` and `top_folder_names` serve the preflight and the Cabinet (the
owner's own act: no policy).

Connector audience (DRIVE_MCP_CONNECTOR_RFC §7.2-7.4): `check_access` reads `CURRENT_DRIVE_AUDIENCE` and hands it to the
policy, so every read under the drive MCP connector needs the scope and the connector access on one chain and every
write is refused; `list_folder` of a drive top outside connector access lists the connector folders; `search` keeps only
results inside both. With no audience nothing changes.
"""
from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple, Union

from ..domain.user_drive import (
    DEFAULT_INBOX_FOLDER,
    MAX_DRIVE_APPEND_FILE_BYTES,
    MOVE_NEEDS_TARGET,
    DeleteOutcome,
    DriveAccessRefused,
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
from ..domain.document_limits import MAX_DRIVE_DOWNLOAD_BYTES
from ..domain.drive_access_op import AccessOp
from ..domain.drive_access_policy import decide, in_connector, in_scope, requires_ancestry
from ..domain.drive_ancestry import Ancestry
from ..domain.drive_audience import CURRENT_DRIVE_AUDIENCE, DriveAudience
from ..domain.drive_capabilities import DriveCapabilities
from ..domain.drive_connection import ROOT_FOLDER, DriveConnection
from ..domain.drive_connection_access import ConnectionAccess
from ..domain.drive_connection_state import ConnectionState
from ..domain.drive_connection_status import DriveConnectionStatus
from ..domain.drive_handle import DriveHandle
from ..domain.drive_index import Nudge
from ..domain.drive_index_params import TICK_DEADLINE_S
from ..domain.drive_mount_table import MountTable
from ..domain.drive_mounts import canonical_path
from ..domain.drive_ref_refusal import DriveRefRefusal
from ..domain.drive_resolved_ref import ResolvedDriveRef
from ..domain.drive_search import MAX_CHAIN_DEPTH
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_task_key, scope_payload
from ..ports.drive_mirror_repository import DriveMirrorRepository
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..utils.logger import logger

if TYPE_CHECKING:
    from .drive_connection_service import DriveConnectionService

LIST_LIMIT = 100
_CLOSED = "\x00closed"       # `_walk` memo: the item answered "not found" to an id read (never a real id)
SEARCH_LIMIT = 25
COUNT_CAP = 10_000


def _chain_of(walked: Sequence[str]) -> Ancestry:
    """The chain of a folder walked from the root down (`walked` = root … folder): item first, root last."""
    return Ancestry(ids=tuple(reversed(walked)), reached_root=True)


def _walked_chain(walked: Sequence[str], reached_root: bool) -> Ancestry:
    """The chain of a folder walked down from `walked[0]`: item first. `reached_root` is False for a walk that began
    at an outer top (G8.3, Δ7): a top is parentless, so the chain ends there unreached."""
    return Ancestry(ids=tuple(reversed(walked)), reached_root=reached_root)


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

    async def mounts(self, user_id: str) -> MountTable:
        """The user's mount tree (MULTI_DRIVE_RFC §11.1), read fresh: `DriveConnectionService.mounts`."""
        return await self._connections.mounts(user_id)

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

    async def _shown(self, handle: DriveHandle, path: str) -> str:
        """`path` (connection-relative) as the refusal that names it prints it: `<mount>/<path>`. Read lazily, on the
        raise path only: the success path adds no read to the access policy's one connection read per operation
        (E-13); a lookup that fails leaves the path as it is."""
        try:
            connection = await self._connections.get(handle.user_id, handle.connection_id)
        except Exception:
            logger.warning("Drive refusal: mount name lookup failed", exc_info=True)
            return path
        return canonical_path(connection.mount_name, path) if connection is not None else path

    # -- access policy (MULTI_DRIVE_RFC §11.3) ------------------------------------------------------------------------

    async def _connection(self, handle: DriveHandle) -> DriveConnection:
        """The handle's connection, read fresh; DriveNotConnectedError when absent or DISCONNECTED."""
        connection = await self._connections.get(handle.user_id, handle.connection_id)
        if connection is None or connection.state is ConnectionState.DISCONNECTED:
            raise DriveNotConnectedError("The drive connection is absent or no longer connected")
        return connection

    async def access(self, handle: DriveHandle) -> Tuple[ConnectionAccess, DriveCapabilities]:
        """The connection's access settings (one fresh connection read, E-13) and its drive's capabilities; the root
        comes from `get_root` (cached by the adapter). DriveNotConnectedError when absent or DISCONNECTED."""
        connection = await self._connection(handle)
        capabilities = await self._drive.capabilities(handle)
        root = await self._drive.get_root(handle)
        return ConnectionAccess.of(connection, root.item_id), capabilities

    async def ancestry(self, handle: DriveHandle, item_id: str, *, memo: Optional[Dict[str, str]] = None) -> Ancestry:
        """The item's parent chain to the root, a parentless item or MAX_CHAIN_DEPTH ids. `memo` (child -> parent id)
        is shared by the walks of one call, so a common parent is read once."""
        return await self._walk(handle, item_id, (await self._drive.get_root(handle)).item_id,
                                memo if memo is not None else {})

    async def _walk(self, handle: DriveHandle, item_id: str, root_id: str, memo: Dict[str, str]) -> Ancestry:
        """The chain of `item_id`. A parent closed to the bot ends it unreached; under the connector audience that
        parent is left out too (DRIVE_MCP_CONNECTOR_RFC §7.3: an unknown ancestor never admits; the index admit does
        not see an unrecorded ancestor either, and a real provider names no parent the account cannot read)."""
        ids = [item_id]
        current = item_id
        while current != root_id and len(ids) < MAX_CHAIN_DEPTH:
            if current not in memo:
                try:
                    memo[current] = (await self._drive.get_change(handle, current)).parent_id
                except DriveItemNotFoundError:
                    if current == item_id:
                        raise
                    memo[current] = _CLOSED       # a parent closed to the bot: the chain ends unreached
            parent = memo[current]
            if not parent or parent == _CLOSED or parent in ids:
                break
            ids.append(parent)
            current = parent
        if (memo.get(current) == _CLOSED and current != item_id
                and CURRENT_DRIVE_AUDIENCE.get() is DriveAudience.CONNECTOR):
            ids.pop()
        return Ancestry(ids=tuple(ids), reached_root=current == root_id)

    async def check_access(self, handle: DriveHandle, op: AccessOp, *, item: Optional[DriveItem] = None,
                           item_id: str = "", chain: Optional[Ancestry] = None,
                           destination: Optional[Ancestry] = None,
                           destination_item: Optional[DriveItem] = None,
                           policy: Optional[Tuple[ConnectionAccess, DriveCapabilities]] = None) -> None:
        """Run the access policy on one operation before its provider call: `item` (or `item_id`) is what the operation
        acts on (a create or save: the destination folder); `chain` its ancestry when the caller has it; `destination`
        / `destination_item` a move's destination folder. Walks a chain only when `requires_ancestry` says so. Raises
        DriveAccessRefused with the policy's line; DriveNotConnectedError for a connection that is gone. `policy` is the
        `access` pair an operation already read: one connection read per operation (E-13)."""
        access, capabilities = policy if policy is not None else await self.access(handle)
        audience = CURRENT_DRIVE_AUDIENCE.get()
        target = item.item_id if item is not None else item_id
        needed = requires_ancestry(op, access, capabilities, audience=audience)
        memo: Dict[str, str] = {}
        if chain is None and needed and target:
            chain = await self._walk(handle, target, access.root_item_id, memo)
        if (op is AccessOp.MOVE and destination is None and destination_item is not None and needed):
            destination = await self._walk(handle, destination_item.item_id, access.root_item_id, memo)
        decision = decide(op, access, capabilities, item.access if item is not None else None, chain,
                          item_id=target, destination=destination,
                          destination_access=destination_item.access if destination_item is not None else None,
                          audience=audience)
        if not decision.allowed:
            logger.info(f"Drive access refused: op={op.value} connection={handle.connection_id[:8]}")
            raise DriveAccessRefused(decision.line)

    async def write_folder_path(self, handle: DriveHandle,
                                policy: Optional[Tuple[ConnectionAccess, DriveCapabilities]] = None) -> Optional[str]:
        """The write folder's connection-relative path: None (read-only), "" (the root), else the folder's path.
        `policy`: the `access` pair the operation already read (no second connection read, E-13)."""
        folder = policy[0].write_folder_id if policy is not None else (await self._connection(handle)).write_folder_id
        if folder is None or folder == ROOT_FOLDER:
            return folder
        return (await self._drive.get_item(handle, folder)).path

    async def picker_folders(self, handle: DriveHandle, folder_id: Optional[str]) -> List[DriveItem]:
        """The Cabinet's folder picker (the owner's own act, no policy): the folder children of `folder_id` (None: the
        root), by name."""
        parent = folder_id if folder_id is not None else (await self._drive.get_root(handle)).item_id
        children = await self._drive.list_children(handle, parent)
        return sorted((c for c in children if c.is_folder), key=lambda c: c.name.casefold())

    async def picker_outer_folders(self, handle: DriveHandle) -> List[DriveItem]:
        """The Cabinet picker's "Outside this drive's root" section (the owner's own act, no policy): the folders of
        `outer_folders`, by name."""
        return sorted((f for f in await self._drive.outer_folders(handle) if f.is_folder),
                      key=lambda f: f.name.casefold())

    async def top_folder_names(self, handle: DriveHandle) -> List[str]:
        """The names of the drive's top-level folders (the rename's shadowing warning, §11.8)."""
        return [f.name for f in await self.picker_folders(handle, None)]

    # -- operations on one connection --------------------------------------------------------------------------------

    async def resolve_folder(self, handle: DriveHandle, path: Optional[str], *, create: bool) -> FolderResolution:
        resolution, _ = await self._resolve(handle, path, create=create)
        return resolution

    async def _resolve(self, handle: DriveHandle, path: Optional[str], *, create: bool,
                       op: Optional[AccessOp] = None,
                       policy: Optional[Tuple[ConnectionAccess, DriveCapabilities]] = None
                       ) -> Tuple[FolderResolution, List[str]]:
        """The resolution plus the item ids of the folders this call created (for the nudge). The access policy runs
        with `op` (default LIST without `create`, CREATE with it) on the walked chain, which costs no extra call: once
        before the first folder is created (nothing is created outside the write folder), else on the final folder
        even when nothing was created, and on the folder where a segment is missing (a folder outside the policy never
        answers "no such folder", RFC §11.3)."""
        op = op if op is not None else (AccessOp.CREATE if create else AccessOp.LIST)
        segments = split_folder_path(path or "")
        current = await self._drive.get_root(handle)
        walked: List[str] = [current.item_id]
        rooted = True            # False once the walk began at an outer top (G8.3)
        created: List[str] = []
        created_ids: List[str] = []
        checked = False
        for index, segment in enumerate(segments):
            match = match_child_folder(await self._drive.list_children(handle, current.item_id), segment)
            if match is None and index == 0:
                # G8.3: a folder outside the root (a shared chain's top); a root child of the same name won above.
                match = match_child_folder(await self._drive.outer_folders(handle), segment)
                if match is not None:
                    walked = []
                    rooted = False
            if match is None:
                if not create:
                    await self.check_access(handle, op, item=current, chain=_walked_chain(walked, rooted),
                                            policy=policy)
                    missing = await self._shown(handle, join_drive_path(current.path, segment))
                    raise DrivePathError(f"No folder '{missing}' on the drive")
                for pending in segments[index:]:     # G12: only names the bot will create; below a missing folder
                    _require_valid_name(pending)     # every later segment is missing too, so none is half-created
                if not checked:
                    await self.check_access(handle, op, item=current, chain=_walked_chain(walked, rooted),
                                            policy=policy)
                    checked = True
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
                        taken = await self._shown(handle, taken)
                        raise DrivePathError(f"'{taken}' already exists as a file, so it cannot be used as "
                                             f"a folder") from exc
            current = match
            walked.append(current.item_id)
        if not checked:
            await self.check_access(handle, op, item=current, chain=_walked_chain(walked, rooted), policy=policy)
        return FolderResolution(folder=current, created=created), created_ids

    async def _refuse_if_read_only(self, handle: DriveHandle, op: AccessOp,
                                   policy: Tuple[ConnectionAccess, DriveCapabilities]) -> None:
        """Before any walk: a drive that accepts no change, or has no write folder, refuses with its line and the
        provider is not called (the decision needs no chain then). `policy`: the operation's one `access` read."""
        access, capabilities = policy
        if not capabilities.writable or access.write_folder_id is None:
            await self.check_access(handle, op, policy=policy)

    async def ensure_folder(self, handle: DriveHandle, folder: str) -> FolderResolution:
        if not split_folder_path(folder):
            raise DrivePathError("A folder name is required")
        await self._refuse_mirror(handle, path=folder)
        policy = await self.access(handle)
        await self._refuse_if_read_only(handle, AccessOp.CREATE, policy)
        resolution, created_ids = await self._resolve(handle, folder, create=True, op=AccessOp.CREATE, policy=policy)
        await self._nudge(handle, created_ids)
        return resolution

    async def save(self, handle: DriveHandle, data: bytes, filename: str, content_type: str,
                   folder: Optional[str]) -> SaveOutcome:
        policy = await self.access(handle)
        await self._refuse_if_read_only(handle, AccessOp.SAVE, policy)
        target = folder if folder and folder.strip() else await self._inbox_path(handle, policy)
        await self._refuse_mirror(handle, path=target)   # before _resolve(create=True) creates anything
        resolution, created_ids = await self._resolve(handle, target, create=True, op=AccessOp.SAVE, policy=policy)
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

    async def _inbox_path(self, handle: DriveHandle,
                          policy: Tuple[ConnectionAccess, DriveCapabilities]) -> str:
        """The default destination: the Inbox inside the connection's write folder (D7). Its one caller, `save`, has
        already refused a drive with no write folder (`_refuse_if_read_only`)."""
        base = await self.write_folder_path(handle, policy)
        if base is None:
            raise RuntimeError("save refuses a drive without a write folder before the inbox is named")
        return join_drive_path(base, DEFAULT_INBOX_FOLDER)

    async def list_folder(self, handle: DriveHandle, folder: Optional[str]) -> ListOutcome:
        if not split_folder_path(folder or ""):
            access, capabilities = await self.access(handle)
            root_chain = Ancestry(ids=(access.root_item_id,), reached_root=True)
            if CURRENT_DRIVE_AUDIENCE.get() is DriveAudience.CONNECTOR:
                if not (in_scope(access, capabilities, root_chain) and in_connector(access, root_chain)):
                    connector = access.connector_access
                    if connector is not None and connector.folder_ids:
                        return await self._connector_tops(handle, (access, capabilities))
                    if connector is None:       # a hidden drive: refused before any name is listed
                        await self.check_access(handle, AccessOp.LIST, chain=root_chain,
                                                item_id=access.root_item_id, policy=(access, capabilities))
                    return await self._scope_tops(handle, access)
            elif not in_scope(access, capabilities, root_chain):
                return await self._scope_tops(handle, access)       # G11: the root is outside the scope
        resolution = await self.resolve_folder(handle, folder, create=False)
        children = await self._drive.list_children(handle, resolution.folder.item_id)
        ordered = sorted(children, key=lambda i: (not i.is_folder, i.name.casefold()))
        return ListOutcome(folder=resolution.folder, items=ordered[:LIST_LIMIT], truncated=len(ordered) > LIST_LIMIT)

    async def _scope_tops(self, handle: DriveHandle, access: ConnectionAccess) -> ListOutcome:
        """The chosen folders as the listing of a drive whose root is outside its scope (G11)."""
        folders = await self._chosen_folders(handle, access)
        return ListOutcome(folder=await self._drive.get_root(handle), items=folders[:LIST_LIMIT],
                           truncated=len(folders) > LIST_LIMIT, scope_tops=True)

    async def _chosen_folders(self, handle: DriveHandle, access: ConnectionAccess) -> List[DriveItem]:
        """The scope folders that can still be read, by name; one that is gone is skipped."""
        found: List[DriveItem] = []
        for folder_id in sorted(access.scope_folder_ids):
            try:
                found.append(await self._drive.get_item(handle, folder_id))
            except DriveItemNotFoundError:
                continue
        return sorted(found, key=lambda i: i.name.casefold())

    async def _connector_tops(self, handle: DriveHandle,
                              policy: Tuple[ConnectionAccess, DriveCapabilities]) -> ListOutcome:
        """The connector folders as the listing of a drive whose root lies outside connector access (§7.4): each one
        that still exists and passes LIST under the audience (a folder moved out of the scope is not listed), by name."""
        access, _ = policy
        connector = access.connector_access
        found: List[DriveItem] = []
        for folder_id in (connector.folder_ids if connector is not None else ()):
            try:
                folder = await self._drive.get_item(handle, folder_id)
                await self.check_access(handle, AccessOp.LIST, item=folder, policy=policy)
            except (DriveItemNotFoundError, DriveAccessRefused):
                continue
            found.append(folder)
        found.sort(key=lambda i: i.name.casefold())
        return ListOutcome(folder=await self._drive.get_root(handle), items=found[:LIST_LIMIT],
                           truncated=len(found) > LIST_LIMIT, connector_tops=True)

    async def scope_folder_paths(self, handle: DriveHandle, limit: int = 5) -> Tuple[List[str], int]:
        """The connection-relative paths of the chosen folders, sorted, at most `limit`, and how many more (the mount
        line's "indexes only"). None under "whole drive" or with no folder chosen (no provider call then); a chosen
        root has the path "" and is not named."""
        connection = await self._connection(handle)
        if connection.scope_whole_drive or not connection.scope_folder_ids:
            return [], 0
        found: List[DriveItem] = []
        for folder_id in connection.scope_folder_ids:
            try:
                found.append(await self._drive.get_item(handle, folder_id))
            except DriveItemNotFoundError:
                continue
        paths = sorted(f.path for f in found if f.path)
        return paths[:limit], max(0, len(paths) - limit)

    async def connector_folder_paths(self, handle: DriveHandle, limit: int = 5) -> Tuple[List[str], int]:
        """The connection-relative paths of the connector folders, sorted, at most `limit`, and how many more (the
        connector's `/` line). ([], 0) for nothing or the whole drive (no provider call then); a folder that is gone is
        skipped."""
        connector = (await self._connection(handle)).connector_access
        if connector is None or not connector.folder_ids:
            return [], 0
        found: List[DriveItem] = []
        for folder_id in connector.folder_ids:
            try:
                found.append(await self._drive.get_item(handle, folder_id))
            except DriveItemNotFoundError:
                continue
        paths = sorted(f.path for f in found if f.path)
        return paths[:limit], max(0, len(paths) - limit)

    async def list_folder_by_id(self, handle: DriveHandle, folder_id: str) -> ListOutcome:
        """A folder given by its `drive:` ref (USER_DRIVE_INDEXING_RFC §26.2, Δ55: search returns folder refs). A file
        ref is refused; a folder the provider does not find raises DriveItemNotFoundError."""
        folder = await self._drive.get_item(handle, folder_id)
        await self.check_access(handle, AccessOp.LIST, item=folder)   # before any text that prints its path
        if not folder.is_folder:
            raise DrivePathError(f"'{await self._shown(handle, folder.path)}' is a file, not a folder; open it instead.")
        children = await self._drive.list_children(handle, folder.item_id)
        ordered = sorted(children, key=lambda i: (not i.is_folder, i.name.casefold()))
        return ListOutcome(folder=folder, items=ordered[:LIST_LIMIT], truncated=len(ordered) > LIST_LIMIT)

    async def search(self, handle: DriveHandle, query: str) -> List[DriveItem]:
        """Provider search, its results filtered to the scope (SEARCH). Nothing is filtered when the scope covers the
        drive; folders chosen: each result's chain is walked (one memo for the call). Under the connector audience a
        result is kept only when the connector access holds on the same chain too."""
        access, capabilities = await self.access(handle)
        audience = CURRENT_DRIVE_AUDIENCE.get()
        connector = audience is DriveAudience.CONNECTOR
        if not requires_ancestry(AccessOp.SEARCH, access, capabilities, audience=audience):
            admitted = in_scope(access, capabilities, None) and (not connector or in_connector(access, None))
            return await self._drive.search(handle, query, SEARCH_LIMIT) if admitted else []
        found = await self._drive.search(handle, query, SEARCH_LIMIT)
        memo: Dict[str, str] = {}
        kept: List[DriveItem] = []
        for item in found:
            chain = await self._walk(handle, item.item_id, access.root_item_id, memo)
            if in_scope(access, capabilities, chain) and (not connector or in_connector(access, chain)):
                kept.append(item)
        return kept

    async def get_item(self, handle: DriveHandle, item_id: str) -> DriveItem:
        """The item's metadata, once the policy allows READ of it (the metadata read comes first: the chain and the
        item's own access need it)."""
        item = await self._drive.get_item(handle, item_id)
        await self.check_access(handle, AccessOp.READ, item=item)
        return item

    async def move(self, handle: DriveHandle, item_id: str, folder: Optional[str], new_name: Optional[str]) -> MoveOutcome:
        if not (folder and folder.strip()) and not (new_name and new_name.strip()):
            raise DrivePathError(MOVE_NEEDS_TARGET)
        new_name = new_name.strip() if new_name and new_name.strip() else None
        if new_name is not None:
            _require_valid_name(new_name)
        moving = bool(folder and folder.strip())
        await self._refuse_root(handle, item_id)
        policy = await self.access(handle)   # the operation's one connection read (E-13), shared by every check below
        await self._refuse_if_read_only(handle, AccessOp.MOVE if moving else AccessOp.RENAME, policy)
        before = await self._drive.get_item(handle, item_id)
        # the source first, before any text that prints its path; the destination is checked by `_resolve`
        await self.check_access(handle, AccessOp.MOVE if moving else AccessOp.RENAME, item=before, policy=policy)
        await self._refuse_mirror(handle, item=before)
        created: List[str] = []
        parent_id: Optional[str] = None
        created_ids: List[str] = []
        if moving:
            await self._refuse_mirror(handle, path=folder)
            await self._refuse_move_into_self(handle, before, str(folder))
            resolution, created_ids = await self._resolve(handle, folder, create=True, op=AccessOp.CREATE,
                                                          policy=policy)
            parent_id, created = resolution.folder.item_id, resolution.created
        after = await self._drive.move(handle, item_id, new_parent_id=parent_id,
                                       new_name=new_name)
        await self._nudge(handle, created_ids + [after.item_id])
        return MoveOutcome(before=before, after=after, created=created)

    async def append_text(self, handle: DriveHandle, item_id: str, text: str) -> UpdateOutcome:
        item = await self._drive.get_item(handle, item_id)
        await self.check_access(handle, AccessOp.APPEND, item=item)   # before any text that prints its path
        await self._refuse_mirror(handle, item=item)
        if item.is_folder or not is_text_file(item.name, item.mime_type):
            raise DrivePathError(f"'{await self._shown(handle, item.path)}' is not a text file; text can only be "
                                 "appended to text files")
        if item.size_bytes > MAX_DRIVE_APPEND_FILE_BYTES:
            raise DrivePathError(f"'{await self._shown(handle, item.path)}' is {format_size(item.size_bytes)}; "
                                 f"appending is limited to "
                                 f"{format_size(MAX_DRIVE_APPEND_FILE_BYTES)} files")
        old = await self._drive.download(handle, item_id)
        try:
            old.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning(f"Drive append refused: '{item.path}' is not UTF-8 text")
            raise DrivePathError(f"'{await self._shown(handle, item.path)}' is not UTF-8 text") from exc
        separator = b"" if not old or old.endswith(b"\n") else b"\n"
        addition = text if text.endswith("\n") else f"{text}\n"
        updated = await self._drive.replace_content(handle, item_id, old + separator + addition.encode("utf-8"),
                                                     item.mime_type or "text/plain")
        await self._nudge(handle, [updated.item_id])
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="append")

    async def replace_with(self, handle: DriveHandle, item_id: str, data: bytes, content_type: str) -> UpdateOutcome:
        item = await self._drive.get_item(handle, item_id)
        await self.check_access(handle, AccessOp.REPLACE, item=item)   # before any text that prints its path
        await self._refuse_mirror(handle, item=item)
        if item.is_folder:
            raise DrivePathError(f"'{await self._shown(handle, item.path)}' is a folder; only a file's content can "
                                 "be replaced")
        updated = await self._drive.replace_content(handle, item_id, data, content_type)
        await self._nudge(handle, [updated.item_id])
        return UpdateOutcome(item=updated, before_size=item.size_bytes, mode="replace")

    async def delete(self, handle: DriveHandle, item_id: str) -> DeleteOutcome:
        await self._refuse_root(handle, item_id)
        item = await self._drive.get_item(handle, item_id)
        await self.check_access(handle, AccessOp.DELETE, item=item)
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

    async def _refuse_move_into_self(self, handle: DriveHandle, item: DriveItem, folder: str) -> None:
        """A folder cannot be moved into itself or its own subtree. Decided on the item's path and
        the requested segments (same `name_key` match as resolution) before any provider call, so
        the missing segments of such a destination are never created."""
        if not item.is_folder or not item.path:
            return
        own = [name_key(s) for s in item.path.split("/")]
        wanted = [name_key(s) for s in split_folder_path(folder)]
        if wanted[:len(own)] == own:
            raise DrivePathError(f"'{await self._shown(handle, item.path)}' cannot be moved into itself")

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
