"""scripts/seed_testmondata.py: seeds a new worktree's .testmondata from the main checkout's."""
import importlib.util
import sqlite3
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_testmondata.py"


def _load():
    spec = importlib.util.spec_from_file_location("seed_testmondata", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_wal_database_is_copied_with_what_is_still_in_the_wal(tmp_path):
    source = tmp_path / "source.db"
    live = sqlite3.connect(source)  # stays open: its last write sits in the -wal file, not in the main file
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("CREATE TABLE t (n INTEGER)")
    live.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    live.commit()
    assert (tmp_path / "source.db-wal").stat().st_size > 0

    dest = tmp_path / "dest.db"
    assert _load().seed(str(source), str(dest)) == "backup"

    copy = sqlite3.connect(dest)
    assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 50
    copy.close()
    live.close()


def test_a_source_sqlite_cannot_open_is_copied_as_it_is(tmp_path):
    source = tmp_path / "source.db"
    source.write_bytes(b"not a database" * 100)
    dest = tmp_path / "dest.db"

    assert _load().seed(str(source), str(dest)) == "copy"
    assert dest.read_bytes() == source.read_bytes()
