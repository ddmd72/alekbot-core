"""Storage of Smart's workspace files (docs/10_rfcs/AGENT_WORKSPACE_RFC.md §5, §13)."""

from abc import ABC, abstractmethod
from typing import List, Optional

from ..domain.workspace import WorkspaceBlob, WorkspaceDeleteMeta, WorkspaceEntry, WorkspaceWriteMeta


class WorkspaceStorePort(ABC):
    """A versioned object store keyed `{user_id}/{path}`.

    Paths arrive validated (domain `parse_workspace_path`); the user id comes from the execution, never from a tool
    argument (§19). A missing object raises FileNotFoundError; a failed generation precondition raises
    WorkspaceConflict (domain/exceptions.py). Every mutation carries `if_generation_match` (0 = must not exist).
    Justified as a port: a system boundary (GCS) with an in-memory fake for service and handler tests.
    Ordering is never promised: callers sort by generation or `updated`.
    """

    @abstractmethod
    async def list(self, user_id: str, prefix: str, include_versions: bool = False) -> List[WorkspaceEntry]:
        """Entries under `prefix`: "" is everything, a prefix ending in "/" is a folder, anything else is exactly
        that file (`projects/a.md` never returns `projects/a.md.bak`). Live versions only unless `include_versions`.
        Order: none. Limit: none (every matching version).
        Legacy: an object without our metadata lists with origin "", write_id/fire None; a non-numeric restored_from
        is None.
        Tenancy: only `{user_id}/` objects; another user's files (`u10/` for `u1`) never appear.
        Errors: nothing matches → []."""

    @abstractmethod
    async def read(self, user_id: str, path: str, generation: Optional[int] = None) -> WorkspaceBlob:
        """The live version, or that generation (live or noncurrent); bytes and entry are of the same generation.
        Tenancy: the user's prefix only.
        Errors: FileNotFoundError when absent, when the generation is unknown or erased, or when that version is gone
        between the metadata and the media read."""

    @abstractmethod
    async def put(
        self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta, if_generation_match: int,
    ) -> WorkspaceEntry:
        """Write a new live version; the previous one becomes noncurrent. Stamps origin, fire, restored_from and a
        fresh write_id; customTime only under scratch/.
        Atomicity: one conditional write; of two writers holding the same generation exactly one wins.
        Idempotency: a client retry of a write that landed is success (its write_id is read back), never a conflict;
        a repeated call with the same generation is WorkspaceConflict.
        Errors: WorkspaceConflict when the live generation is not `if_generation_match` (0: when a live version
        exists; noncurrent versions do not count); other storage errors propagate."""

    @abstractmethod
    async def delete(self, user_id: str, path: str, if_generation_match: int, meta: WorkspaceDeleteMeta) -> None:
        """Stamp `meta` (deleted_by, deleted_fire, renamed_to, a fresh delete_id; absent keys cleared) on the live
        version, then make it noncurrent (still restorable; never erased).
        Atomicity: two requests (metadata patch, delete), both conditional on the same generation. A delete that
        raises leaves no deleted_* stamp on a version another write makes noncurrent (P11-G4-1: not yet true when
        that write lands between the two requests). A delete that fails for another reason may leave the stamp on the
        still-live version (§5.3 residual; renderers ignore it on live objects).
        Idempotency: a client retry of a delete that landed is success (its delete_id is read back); a second call
        raises FileNotFoundError.
        Errors: FileNotFoundError when there is no live object; WorkspaceConflict when its generation differs."""
