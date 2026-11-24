"""WorkspaceService.write_background — a file stored by code without a model turn (the external task delivery): created
once, never overwritten; a retry of the same write (same origin) is told from a file of another origin; a concurrent
writer is looked at again; an oversize text is refused. The result file is an ordinary workspace file (no mark)."""
import pytest

from src.domain.exceptions import WorkspaceConflict, WorkspaceRefused
from src.domain.workspace import MAX_FILE_BYTES, BackgroundWrite, WorkspaceWriteMeta
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "usr-1"
P = "references/external_tasks/2026-11-23-a1b2c3d4.md"
ORIGIN = "background:external_task:a1"


class _RacingStore(InMemoryWorkspaceStore):
    """The first `puts` raise a conflict after another writer took the path."""

    def __init__(self, conflicts: int, rival_origin: str) -> None:
        super().__init__()
        self.conflicts, self.rival_origin = conflicts, rival_origin

    async def put(self, user_id, path, data, meta, if_generation_match):
        if self.conflicts:
            self.conflicts -= 1
            self.seed(user_id, path, "rival", origin=self.rival_origin)
            raise WorkspaceConflict(path)
        return await super().put(user_id, path, data, meta, if_generation_match)


async def test_the_file_is_written_once_and_a_retry_is_told_from_a_stranger():
    store = InMemoryWorkspaceStore()
    svc = WorkspaceService(store)
    assert await svc.write_background(USER, P, "result", origin=ORIGIN) is BackgroundWrite.WRITTEN
    [entry] = store.versions_of(USER, P)
    assert entry.origin == ORIGIN and store.live_text(USER, P) == "result"
    assert await svc.write_background(USER, P, "other", origin=ORIGIN) is BackgroundWrite.REUSED
    assert await svc.write_background(USER, P, "other", origin="background:external_task:zz") is BackgroundWrite.TAKEN
    assert store.live_text(USER, P) == "result" and len(store.versions_of(USER, P)) == 1


async def test_a_file_of_the_owner_on_the_path_is_left_alone():
    store = InMemoryWorkspaceStore()
    store.seed(USER, P, "mine")
    assert await WorkspaceService(store).write_background(USER, P, "x", origin=ORIGIN) is BackgroundWrite.TAKEN
    assert store.live_text(USER, P) == "mine"


async def test_an_oversize_text_is_refused_and_nothing_is_written():
    store = InMemoryWorkspaceStore()
    with pytest.raises(WorkspaceRefused):
        await WorkspaceService(store).write_background(USER, P, "x" * (MAX_FILE_BYTES + 1), origin=ORIGIN)
    assert await store.list(USER, "") == []


async def test_a_concurrent_writer_of_the_same_write_is_reused_and_a_stranger_is_taken():
    same = WorkspaceService(_RacingStore(1, ORIGIN))
    assert await same.write_background(USER, P, "x", origin=ORIGIN) is BackgroundWrite.REUSED
    other = WorkspaceService(_RacingStore(1, "chat"))
    assert await other.write_background(USER, P, "x", origin=ORIGIN) is BackgroundWrite.TAKEN


async def test_two_conflicts_without_a_live_file_end_as_taken():
    class _Ghost(_RacingStore):
        async def put(self, user_id, path, data, meta, if_generation_match):
            self.conflicts -= 1
            raise WorkspaceConflict(path)

    assert await WorkspaceService(_Ghost(2, "chat")).write_background(USER, P, "x", origin=ORIGIN) \
        is BackgroundWrite.TAKEN
