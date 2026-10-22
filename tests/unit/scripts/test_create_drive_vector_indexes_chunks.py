"""create_drive_vector_indexes.sh creates the chunk vector indexes too (USER_DRIVE_INDEXING_RFC §12.5, §24.6): the
`vector` field of the chunk collection under user_id and user_id + kind, after the two card indexes."""
import pytest
import os
import subprocess
from pathlib import Path

pytestmark = pytest.mark.datafile

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "create_drive_vector_indexes.sh"
_STUB = '#!/bin/bash\necho "$*" >> "$STUB_DIR/calls"\necho "Create request issued"\n'


def _calls(tmp_path, **env_extra):
    (tmp_path / "gcloud").write_text(_STUB)
    (tmp_path / "gcloud").chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "STUB_DIR": str(tmp_path), "PROJECT_ID": "p",
           **env_extra}
    proc = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return (tmp_path / "calls").read_text().splitlines()


def test_six_indexes_two_of_them_on_the_chunk_vector(tmp_path):
    calls = _calls(tmp_path)
    assert len(calls) == 6   # + the two Search v2 hint vector indexes (§25.2.5)
    chunk_calls = [c for c in calls if "--collection-group=development_drive_index_chunks" in c]
    assert len(chunk_calls) == 2
    assert all("field-path=vector" in c and '"dimension":"768"' in c for c in chunk_calls)
    assert "field-path=kind" not in chunk_calls[0] and "field-path=kind" in chunk_calls[1]


def test_the_prefix_applies_to_the_chunk_collection(tmp_path):
    calls = _calls(tmp_path, COLLECTION_PREFIX="test_")
    assert sum("--collection-group=test_drive_index_chunks" in c for c in calls) == 2
