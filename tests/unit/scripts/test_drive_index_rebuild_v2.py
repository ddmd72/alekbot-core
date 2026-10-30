"""scripts/infrastructure/drive_index_rebuild.py under Search v2 (USER_DRIVE_INDEXING_RFC §25.8, D30): the rebuild is
the only migration (a dry run names a v1 index), `--hints-only` is the rollback's step 2 (every hint document, under
the lease, before the tag deploy), and `--all-users` visits every sync state."""
import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import DriveHintDoc, HintItem
from src.domain.drive_index import CURRENT_VERSIONS, DriveSyncState

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
A, B = ConnectionScope("a", "a-c"), ConnectionScope("b", "b-c")
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_rebuild.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_index_rebuild_v2", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hint(user, item, index):
    return DriveHintDoc(user_id=user, connection_id=f"{user}-c", item_id=item, generation=1, index=index, kind="pdf", chunk_text_hash="h",
                        items=(HintItem(text="lease", type="keyword", evidence="lease"),), hint_keys=("w:leas",),
                        passages_version=1)


async def _state(repo, user, applied):
    await repo.save_sync_state(DriveSyncState(user_id=user, connection_id=f"{user}-c", epoch=2, root_item_id="root", created_at=T0,
                                              applied_versions=applied))


async def _hints(repo, user, n):
    docs = [_hint(user, f"i{k // 3}", k % 3) for k in range(n)]
    await repo.write_hints(docs, [[1.0, 0.0]] * n)


async def test_a_dry_run_names_a_v1_index_awaiting_its_rebuild(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", {"policy": 2})
    code, message = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r")
    # §26.7 (Δ65): the gate now stops any index built before `accuracy`, not only a v1 one
    assert code == 0 and "built under an older index version" in message and "D30" in message and "--apply" in message


async def test_a_dry_run_of_a_v2_index_does_not_call_it_v1(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    _, message = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r")
    assert "older index version" not in message and "D30" not in message


async def test_hints_only_dry_run_counts_and_writes_nothing(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    await _hints(repo, "u", 7)
    code, message = await script.delete_hints(repo, SCOPE, apply=False, now=T0, owner="r")
    assert code == 0 and "7 hint documents" in message
    assert len(await repo.hints_page(SCOPE, None, 100)) == 7


async def test_hints_only_deletes_every_hint_document_and_nothing_else(script, monkeypatch):
    monkeypatch.setattr(script, "_HINT_PAGE", 2)   # several pages
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    await _hints(repo, "u", 5)
    await _hints(repo, "other", 2)
    code, message = await script.delete_hints(repo, SCOPE, apply=True, now=T0, owner="r")
    state = await repo.get_sync_state(SCOPE)
    assert code == 0 and "deleted 5" in message
    assert await repo.hints_page(SCOPE, None, 100) == []
    assert len(await repo.hints_page(ConnectionScope("other", "other-c"), None, 100)) == 2
    assert not state.purging and state.lease_owner is None


async def test_hints_only_waits_for_a_running_tick(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    await _hints(repo, "u", 3)
    await repo.acquire_lease(SCOPE, "tick", T0, T0 + timedelta(minutes=31), create=False)
    code, message = await script.delete_hints(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 1 and "pause the drive queue" in message
    assert len(await repo.hints_page(SCOPE, None, 100)) == 3


async def test_hints_only_without_a_sync_state_still_deletes(script):
    repo = InMemoryDriveIndexRepository()
    await _hints(repo, "u", 2)
    code, message = await script.delete_hints(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 0 and await repo.hints_page(SCOPE, None, 100) == []


async def test_all_users_runs_the_step_for_each_and_reports_the_worst_code(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "a", {"policy": 2})
    await _state(repo, "b", {"policy": 2})
    await repo.acquire_lease(B, "tick", T0, T0 + timedelta(minutes=31), create=False)
    code, lines = await script.run(repo, [A, B], hints_only=False, apply=True, now=T0)
    assert code == 1 and lines[0].startswith("a: marked purging") and lines[1].startswith("b: a tick holds")
    assert (await repo.get_sync_state(A)).purging and not (await repo.get_sync_state(B)).purging
