"""create_task_search_vector_indexes.sh: four vector indexes (content/context x user_id / user_id+status); an index that
already exists is fine, any other failure stops the script. Runs against a stub `gcloud`."""
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.datafile

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "create_task_search_vector_indexes.sh"

_STUB = """#!/bin/bash
n=$(cat "$STUB_DIR/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$STUB_DIR/count"
echo "$*" >> "$STUB_DIR/calls"
case "$(sed -n "${n}p" "$STUB_DIR/answers")" in
  ok) exit 0 ;;
  exists) echo "ALREADY_EXISTS: index already exists" >&2; exit 1 ;;
  *) echo "PERMISSION_DENIED: caller lacks permission" >&2; exit 1 ;;
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


@pytest.mark.parametrize("answers", [("ok",) * 4, ("exists",) * 4])
def test_created_or_already_existing_is_success(tmp_path, answers):
    proc, calls = _run(tmp_path, *answers)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 4
    assert all("--collection-group=development_task_search_index" in c for c in calls)
    assert sum("field-path=content_vector" in c for c in calls) == 2
    assert sum("field-path=context_vector" in c for c in calls) == 2
    assert sum("field-path=status" in c for c in calls) == 2


def test_any_other_failure_stops_with_a_non_zero_exit(tmp_path):
    proc, calls = _run(tmp_path, "ok", "denied")
    assert proc.returncode != 0
    assert len(calls) == 2
    assert "PERMISSION_DENIED" in proc.stderr
