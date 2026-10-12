"""create_drive_vector_indexes.sh (USER_DRIVE_INDEXING_RFC §12.5): an index that already exists is fine, any other
failure stops the script with a non-zero exit, so a broken vector index never passes as "created". Runs the script
against a stub `gcloud` that answers from a script of exit codes."""
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "create_drive_vector_indexes.sh"

_STUB = """#!/bin/bash
n=$(cat "$STUB_DIR/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_DIR/count"
echo "$*" >> "$STUB_DIR/calls"
case "$(sed -n "${n}p" "$STUB_DIR/answers")" in
  ok) echo "Create request issued"; exit 0 ;;
  exists) echo "ERROR: (gcloud.firestore.indexes.composite.create) ALREADY_EXISTS: index already exists" >&2; exit 1 ;;
  *) echo "ERROR: (gcloud.firestore.indexes.composite.create) PERMISSION_DENIED: caller lacks permission" >&2; exit 1 ;;
esac
"""


def _run(tmp_path, *answers):
    (tmp_path / "gcloud").write_text(_STUB)
    (tmp_path / "gcloud").chmod(0o755)
    (tmp_path / "answers").write_text("\n".join(answers) + "\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "STUB_DIR": str(tmp_path), "PROJECT_ID": "p"}
    proc = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return proc, calls


# Four indexes: two card vector indexes, then two chunk vector indexes (§24.6).
@pytest.mark.parametrize("answers", [("ok",) * 4, ("exists",) * 4, ("exists", "ok", "exists", "ok")])
def test_created_or_already_existing_is_success(tmp_path, answers):
    proc, calls = _run(tmp_path, *answers)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 4


def test_any_other_failure_stops_with_a_non_zero_exit(tmp_path):
    proc, calls = _run(tmp_path, "denied", "ok")
    assert proc.returncode != 0
    assert len(calls) == 1
    assert "PERMISSION_DENIED" in proc.stderr


def test_a_failure_on_the_second_index_fails_the_run(tmp_path):
    proc, _ = _run(tmp_path, "ok", "denied")
    assert proc.returncode != 0


def test_a_failure_on_the_last_chunk_index_fails_the_run(tmp_path):
    proc, calls = _run(tmp_path, "ok", "ok", "ok", "denied")
    assert proc.returncode != 0
    assert len(calls) == 4
    assert "PERMISSION_DENIED" in proc.stderr
