"""scripts/infrastructure/drive_index_smoke.py runs every transcript-mirror read once when it is given the mirror
repository (USER_DRIVE_INDEXING_RFC §27.15 step 8c, Task 20A): the records' queue, wake-ups and cursor read, the roots, and
every ledger query, none of them writing. Without the mirror repository the list is the one the older smoke tests pin."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import FailedPrecondition

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "drive_index_smoke.py"
MIRROR_CHECKS = [
    "mirror_queue", "mirror_wakeups", "transcript_records[after]", "mirror roots", "mirror get_root", "mirror get_docs",
    "mirror by_mirror_item", "mirror ready_marks", "mirror children", "mirror folder_docs", "mirror folder_docs[cursor]",
    "mirror scan_page", "mirror scan_page[cursor]", "mirror detached_due", "mirror archived_newest",
    "mirror archived_by_keys[archive]", "mirror archived_by_keys[name]", "mirror pinned", "mirror pinned_keys",
]


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("drive_index_smoke", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mirror():
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    db = NameAwareDb()
    return db, FirestoreDriveMirrorRepository(db, env)


async def test_smoke_runs_every_new_read(smoke, mirror, mock_env_config):
    mdb, mrepo = mirror
    db = ChunkDb()
    repo = make_chunk_repo(db, mock_env_config)
    names = [name for name, _ in smoke.checks(repo, "smoke-user", mrepo)]
    assert names[-len(MIRROR_CHECKS):] == MIRROR_CHECKS
    assert [name for name, _ in smoke.checks(repo, "smoke-user")] == names[:-len(MIRROR_CHECKS)]   # opt-in
    before = len(db.cols[RECS].executed)
    assert await smoke.run_smoke(repo, "smoke-user", mrepo) == []
    mirror_record_reads = [ops for ops in db.cols[RECS].executed[before:]
                           if any(op[0] == "where" and op[1] in ("mirror_due", "mirror_retry_at") for op in ops)]
    assert len(mirror_record_reads) == 2
    assert len(mdb.cols[ROOTS].executed) == 1                      # roots (the others are document reads)
    # mirror item 1, marks 1, children 1, folder walk 2, scan 2, detached 2 (never checked, old), newest 1, by keys 2,
    # pinned 2, pinned_keys 2: the pins send one query per pinning condition
    assert len(mdb.cols[LEDGER].executed) == 16
    assert all(not col.docs for col in mdb.cols.values())
    assert mdb.commits == [] and mdb.transactions == []            # nothing written


async def test_a_missing_ledger_index_is_named_per_query(smoke, mirror, mock_env_config):
    mdb, mrepo = mirror
    repo = make_chunk_repo(ChunkDb(), mock_env_config)
    mdb.collection(LEDGER).fail = FailedPrecondition("The query requires an index.")
    failures = await smoke.run_smoke(repo, "smoke-user", mrepo)
    failed = {name for name, _ in failures}
    assert {"mirror ready_marks", "mirror children", "mirror folder_docs", "mirror scan_page", "mirror detached_due",
            "mirror archived_newest", "mirror pinned", "mirror pinned_keys"} <= failed
    assert "mirror roots" not in failed and "mirror_queue" not in failed
    assert all("requires an index" in error for _, error in failures)
