"""drive_mirror_status.py (T2 Task 21E): read-only, output only to a given directory (scripts/memory/ by default).
In-memory repositories only."""
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_index import DriveIndexStoreUnavailable
from src.domain.drive_mirror import (
    KIND_ARCHIVE, KIND_FILE, KIND_FOLDER, MIRROR_ACTIVE, MIRROR_ARCHIVED, MIRROR_DETACHED, MirrorDoc, MirrorRoot,
)

from tests.unit.services.drive_index_fakes import T0

_DIR = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"


@pytest.fixture
def status():
    spec = importlib.util.spec_from_file_location("drive_mirror_status", _DIR / "drive_mirror_status.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(item, kind=KIND_FILE, state=MIRROR_ACTIVE, **kw):
    return MirrorDoc(user_id="u", drive_identity="d1", kind=kind, source_item_id=item, source_identity=f"d1:{item}",
                     state=state, **kw)


async def _world():
    repo = InMemoryDriveMirrorRepository()
    await repo.save_root(MirrorRoot(
        user_id="u", drive_identity="d1", root_item_id="r1", root_name="_Transcripts", created_at=T0 - timedelta(days=30),
        audited_at=T0 - timedelta(days=2), seed_version="s3", seed_cursor="s3|x", scan_cursor="f|k", audit_folder_key="u:folder:f",
        audit_page_cursor="@confirm", orphans_reported=True, quota_hold_until=T0 + timedelta(hours=2),
        root_gone_at=T0 - timedelta(hours=3), previous_root_ids=("old",), readme_blocked_at=T0 - timedelta(days=1)))
    await repo.save_doc(_doc("f1"))
    await repo.save_doc(_doc("f2", state=MIRROR_ARCHIVED, content_key="k1"))
    await repo.save_doc(_doc("f3", state=MIRROR_DETACHED, content_key="k2"))                     # never checked: due
    await repo.save_doc(_doc("f4", state=MIRROR_DETACHED, content_key="k3", checked_at=T0 - timedelta(days=1)))
    await repo.save_doc(_doc("f5", gone_pending=True, content_key="k4", mark_ready_at=T0 - timedelta(minutes=1)))   # ripe
    await repo.save_doc(_doc("f6", gone_pending=True, content_key="k5", mark_ready_at=T0 + timedelta(hours=1)))     # held
    await repo.save_doc(_doc("f7", failed_at=T0 - timedelta(days=1), attempts=5))                                 # parked
    await repo.save_doc(_doc("f8", retry_at=T0 + timedelta(minutes=30), attempts=1))                              # backing off
    await repo.save_doc(_doc("fold", kind=KIND_FOLDER))
    await repo.save_doc(_doc("day", kind=KIND_ARCHIVE))
    return repo


async def test_report_counts_states_marks_parked_pins_and_due(status, tmp_path):
    repo = await _world()
    code, path = await status.run(repo, ["u"], T0, out_dir=tmp_path)
    user = json.loads(path.read_text())["users"][0]
    ledger = user["roots"][0]["ledger"]
    assert code == 0 and path.parent == tmp_path
    assert user["pinned_key_count"] == 5 and user["pinned_keys"] == ["k1", "k2", "k3", "k4", "k5"]
    assert ledger["documents"] == 10
    assert ledger["per_kind_state"] == {"archive/active": 1, "file/active": 5, "file/archived": 1, "file/detached": 2,
                                        "folder/active": 1}
    assert (ledger["marks_waiting"], ledger["marks_ripe"], ledger["marks_not_ripe"]) == (2, 1, 1)
    assert ledger["parked"] == 1 and ledger["backing_off"] == 1 and ledger["detached_due"] == 1


async def test_report_carries_the_roots_cursors_clocks_holds_and_loss_marks(status, tmp_path):
    repo = await _world()
    _, path = await status.run(repo, ["u"], T0, out_dir=tmp_path)
    root = json.loads(path.read_text())["users"][0]["roots"][0]
    assert root["cursors"] == {"seed_version": "s3", "seed": "s3|x", "scan": "f|k", "audit_folder": "u:folder:f",
                               "audit_page": "@confirm"}
    assert root["clocks"]["audit_due"] is False and root["clocks"]["audited_at"] is not None
    assert root["holds"]["quota_hold_active"] is True and root["holds"]["readme_blocked_at"] is not None
    assert root["loss"]["root_gone_at"] is not None and root["previous_roots"] == 1
    assert root["ledger_reset_under_way"] is False and root["orphans_reported"] is True


async def test_reset_under_way_is_reported(status, tmp_path):
    repo = InMemoryDriveMirrorRepository()
    await repo.save_root(MirrorRoot(user_id="u", drive_identity="d1", root_item_id="old", root_name="_Transcripts",
                                    previous_root_ids=("old",)))
    _, path = await status.run(repo, ["u"], T0, out_dir=tmp_path)
    assert json.loads(path.read_text())["users"][0]["roots"][0]["ledger_reset_under_way"] is True


async def test_it_writes_nothing_to_the_repository(status, tmp_path):
    repo = await _world()
    docs, roots = dict(repo.docs), dict(repo.root_docs)
    await status.run(repo, ["u"], T0, out_dir=tmp_path)
    assert repo.docs == docs and repo.root_docs == roots


async def test_a_user_with_nothing_is_an_empty_entry(status, tmp_path):
    _, path = await status.run(InMemoryDriveMirrorRepository(), ["nobody"], T0, out_dir=tmp_path)
    assert json.loads(path.read_text())["users"][0] == {"user_id": "nobody", "roots": [], "pinned_keys": [],
                                                        "pinned_key_count": 0}


async def test_a_missing_index_is_recorded_and_exits_1(status, tmp_path):
    repo = await _world()

    async def broken(user_id):
        raise DriveIndexStoreUnavailable("index not ready")
    repo.pinned_keys = broken
    code, path = await status.run(repo, ["u"], T0, out_dir=tmp_path)
    assert code == 1 and "store unavailable" in json.loads(path.read_text())["users"][0]["error"]


def test_the_default_output_is_scripts_memory_and_is_ignored_by_git(status):
    assert status.OUT_DIR == Path(__file__).resolve().parents[3] / "scripts" / "memory"
    gitignore = (Path(__file__).resolve().parents[3] / ".gitignore").read_text()
    assert "scripts/memory" in gitignore


def test_the_script_has_no_write_calls():
    src = (_DIR / "drive_mirror_status.py").read_text()
    for call in ("save_doc", "save_root", "delete_doc", "mark_gone", "clear_mark", "hold_mark", "mark_root_gone", ".delete("):
        assert call not in src
