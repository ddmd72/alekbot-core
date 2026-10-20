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
    """

    @abstractmethod
    async def list(self, user_id: str, prefix: str, include_versions: bool = False) -> List[WorkspaceEntry]:
        """Entries under `prefix`: "" is everything, a prefix ending in "/" is a folder, anything else is exactly
        that file (`projects/a.md` never returns `projects/a.md.bak`). Live versions only unless `include_versions`."""

    @abstractmethod
    async def read(self, user_id: str, path: str, generation: Optional[int] = None) -> WorkspaceBlob:
        """The live version, or that generation (live or noncurrent). FileNotFoundError when absent."""

    @abstractmethod
    async def put(
        self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta, if_generation_match: int,
    ) -> WorkspaceEntry:
        """Write a new live version; the previous one becomes noncurrent. WorkspaceConflict when the live generation
        is not `if_generation_match` (0: when the path already exists)."""

    @abstractmethod
    async def delete(self, user_id: str, path: str, if_generation_match: int, meta: WorkspaceDeleteMeta) -> None:
        """Stamp `meta` on the live version, then make it noncurrent (still restorable; never erased).
        FileNotFoundError when there is no live object; WorkspaceConflict when its generation differs."""
