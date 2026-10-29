"""create_drive_scoped_vector_indexes.sh (USER_DRIVE_INDEXING_RFC Δ246): a dry run by default; APPLY=1 creates the
chunk `vector` and the hint `hint_vector` indexes under user_id + item_id; an index that already exists is fine, any
other failure stops the run with a non-zero exit. Runs the script with a stub gcloud first on PATH."""
import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "create_drive_scoped_vector_indexes.sh"
_STUB = '#!/bin/bash\necho "$*" >> "$STUB_DIR/calls"\n' \
        'if [[ -n "${STUB_FAIL:-}" ]]; then echo "$STUB_FAIL"; exit 1; fi\necho "Create request issued"\n'


def _run(tmp_path, **env_extra):
    (tmp_path / "gcloud").write_text(_STUB)
    (tmp_path / "gcloud").chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "STUB_DIR": str(tmp_path), "PROJECT_ID": "p",
           **env_extra}
    proc = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return proc, calls


def test_a_dry_run_creates_nothing(tmp_path):
    proc, calls = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert calls == [] and proc.stdout.count("dry run, APPLY=1 creates it") == 2


def test_apply_creates_the_chunk_and_the_hint_index_under_user_id_and_item_id(tmp_path):
    proc, calls = _run(tmp_path, APPLY="1")
    assert proc.returncode == 0, proc.stderr
    chunk, hint = calls
    for call, collection, vector in ((chunk, "development_drive_index_chunks", "vector"),
                                     (hint, "development_drive_index_hints", "hint_vector")):
        assert f"--collection-group={collection}" in call and "--database=us-production" in call
        assert call.index("field-path=user_id") < call.index("field-path=item_id") < call.index(
            '"dimension":"768"') < call.index(f",field-path={vector} ")


def test_the_prefix_applies(tmp_path):
    _, calls = _run(tmp_path, APPLY="1", COLLECTION_PREFIX="test_")
    assert ["--collection-group=test_drive_index_chunks" in calls[0],
            "--collection-group=test_drive_index_hints" in calls[1]] == [True, True]


def test_already_existing_is_success_and_any_other_failure_stops(tmp_path):
    proc, calls = _run(tmp_path, APPLY="1", STUB_FAIL="ALREADY_EXISTS: index exists")
    assert proc.returncode == 0 and len(calls) == 2
    (tmp_path / "calls").unlink()
    proc, calls = _run(tmp_path, APPLY="1", STUB_FAIL="PERMISSION_DENIED")
    assert proc.returncode != 0 and len(calls) == 1
