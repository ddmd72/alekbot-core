"""In-memory WorkspaceStorePort with the GCS semantics the RFC relies on (AGENT_WORKSPACE_RFC §5.3): generations,
noncurrent versions carrying their deletion metadata, a precondition on every mutation, exact-name file lists.
Kept honest by tests/unit/adapters/test_workspace_store_contract.py, which runs the same contract against
GcsWorkspaceAdapter."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple, Union

from src.domain.exceptions import WorkspaceConflict
from src.domain.workspace import WorkspaceBlob, WorkspaceDeleteMeta, WorkspaceEntry, WorkspaceWriteMeta
from src.ports.workspace_store_port import WorkspaceStorePort

T0 = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


class InMemoryWorkspaceStore(WorkspaceStorePort):

    def __init__(self) -> None:
        # (user_id, path) -> [[entry, data], …], oldest first
        self.objects: Dict[Tuple[str, str], List[list]] = {}
        self.calls: List[Tuple[str, str]] = []                  # (method, path or prefix)
        self.before_put: List[Callable[[], None]] = []          # one runs before each put: a concurrent writer
        self.fail_next: Dict[str, List[Exception]] = {}         # method -> exceptions raised by its next calls
        self.delete_gate: Optional[asyncio.Event] = None        # delete waits on it (cancellation tests)
        self._generation = 100
        self._now = T0

    # -- test helpers ------------------------------------------------------------------------------------------

    def seed(self, user_id: str, path: str, data: Union[str, bytes], *, origin: str = "chat",
             fire: Optional[str] = None, size: Optional[int] = None) -> WorkspaceEntry:
        """Write a new live version directly (no precondition), as another conversation or run would."""
        raw = data.encode("utf-8") if isinstance(data, str) else data
        entry = self._write(user_id, path, raw, WorkspaceWriteMeta(origin=origin, fire=fire))
        if size is not None:
            entry = entry.model_copy(update={"size": size})
            self.objects[(user_id, path)][-1][0] = entry
        return entry

    def live_text(self, user_id: str, path: str) -> Optional[str]:
        live = self._live(user_id, path)
        return None if live is None else live[1].decode("utf-8")

    def versions_of(self, user_id: str, path: str) -> List[WorkspaceEntry]:
        return [item[0] for item in self.objects.get((user_id, path), [])]

    # -- internals ---------------------------------------------------------------------------------------------

    def _tick(self) -> datetime:
        self._now += timedelta(minutes=1)
        return self._now

    def _live(self, user_id: str, path: str) -> Optional[list]:
        for item in reversed(self.objects.get((user_id, path), [])):
            if item[0].live:
                return item
        return None

    def _write(self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta) -> WorkspaceEntry:
        now = self._tick()
        live = self._live(user_id, path)
        if live is not None:
            live[0] = live[0].model_copy(update={"live": False, "deleted_at": now})
        self._generation += 1
        entry = WorkspaceEntry(
            path=path, generation=self._generation, size=len(data), updated=now, origin=meta.origin,
            fire=meta.fire, write_id=f"w{self._generation}", restored_from=meta.restored_from,
        )
        self.objects.setdefault((user_id, path), []).append([entry, data])
        return entry

    async def _enter(self, method: str, target: str) -> None:
        await asyncio.sleep(0)  # every call yields, like a network call
        self.calls.append((method, target))
        queued = self.fail_next.get(method)
        if queued:
            raise queued.pop(0)

    # -- port --------------------------------------------------------------------------------------------------

    async def list(self, user_id: str, prefix: str, include_versions: bool = False) -> List[WorkspaceEntry]:
        await self._enter("list", prefix)
        exact = bool(prefix) and not prefix.endswith("/")
        out: List[WorkspaceEntry] = []
        for (uid, path), items in sorted(self.objects.items()):
            if uid != user_id or (path != prefix if exact else not path.startswith(prefix)):
                continue
            out.extend(item[0] for item in items if include_versions or item[0].live)
        return out

    async def read(self, user_id: str, path: str, generation: Optional[int] = None) -> WorkspaceBlob:
        await self._enter("read", path)
        for entry, data in self.objects.get((user_id, path), []):
            if (generation is None and entry.live) or entry.generation == generation:
                return WorkspaceBlob(data=data, entry=entry)
        raise FileNotFoundError(path)

    async def put(self, user_id: str, path: str, data: bytes, meta: WorkspaceWriteMeta,
                  if_generation_match: int) -> WorkspaceEntry:
        await self._enter("put", path)
        if self.before_put:
            self.before_put.pop(0)()
        live = self._live(user_id, path)
        if (live[0].generation if live else 0) != if_generation_match:
            raise WorkspaceConflict(path)
        return self._write(user_id, path, data, meta)

    async def delete(self, user_id: str, path: str, if_generation_match: int, meta: WorkspaceDeleteMeta) -> None:
        await self._enter("delete", path)
        if self.delete_gate is not None:
            await self.delete_gate.wait()
        live = self._live(user_id, path)
        if live is None:
            raise FileNotFoundError(path)
        if live[0].generation != if_generation_match:
            raise WorkspaceConflict(path)
        live[0] = live[0].model_copy(update={
            "live": False, "deleted_at": self._tick(), "deleted_by": meta.deleted_by,
            "deleted_fire": meta.deleted_fire, "renamed_to": meta.renamed_to, "delete_id": f"d{if_generation_match}",
        })
