"""drive_index_rebuild.py skips a state already rebuilt under the current versions (MIGRATION_GATE_KEY present)
unless --force; the dry run names the skip."""
import importlib.util
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import CURRENT_VERSIONS, DriveSyncState

from src.domain.connection_scope import ConnectionScope

from tests.unit.services.drive_index_fakes import T0

SCOPE = ConnectionScope("u", "u-c")   # the connection the fixtures belong to (MULTI_DRIVE_RFC §8)
A, B = ConnectionScope("a", "a-c"), ConnectionScope("b", "b-c")
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_rebuild.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drive_index_rebuild_skip", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _state(repo, user, applied):
    await repo.save_sync_state(DriveSyncState(user_id=user, connection_id=f"{user}-c", epoch=2, root_item_id="root", created_at=T0,
                                              applied_versions=applied))


async def test_apply_skips_a_state_already_rebuilt(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 0 and message.startswith("skipped") and "--force" in message
    assert not (await repo.get_sync_state(SCOPE)).purging


async def test_force_rebuilds_it_anyway(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    code, message = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r", force=True)
    assert code == 0 and message.startswith("marked purging")
    assert (await repo.get_sync_state(SCOPE)).purging


async def test_apply_still_rebuilds_a_state_without_the_gate_key(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", {"policy": 2})
    code, _ = await script.rebuild(repo, SCOPE, apply=True, now=T0, owner="r")
    assert code == 0 and (await repo.get_sync_state(SCOPE)).purging


async def test_dry_run_says_which_state_would_be_skipped_and_why(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    code, message = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r")
    assert code == 0 and "would skip" in message and "already rebuilt" in message and "--force" in message
    _, forced = await script.rebuild(repo, SCOPE, apply=False, now=T0, owner="r", force=True)
    assert "would mark" in forced


async def test_all_users_skips_the_rebuilt_ones_only(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "a", {"policy": 2})
    await _state(repo, "b", dict(CURRENT_VERSIONS))
    code, lines = await script.run(repo, [A, B], hints_only=False, apply=True, now=T0)
    assert code == 0 and lines[0].startswith("a: marked purging") and lines[1].startswith("b: skipped")
    code, lines = await script.run(repo, [A, B], hints_only=False, apply=True, now=T0, force=True)
    assert (await repo.get_sync_state(B)).purging


async def test_hints_only_ignores_force_and_the_gate(script):
    repo = InMemoryDriveIndexRepository()
    await _state(repo, "u", dict(CURRENT_VERSIONS))
    code, lines = await script.run(repo, [SCOPE], hints_only=True, apply=True, now=T0)
    assert code == 0 and lines[0].startswith("deleted")
