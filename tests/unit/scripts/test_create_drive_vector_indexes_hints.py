"""create_drive_vector_indexes.sh creates the Search v2 hint vector indexes too (USER_DRIVE_INDEXING_RFC §25.2.5): the
`hint_vector` field of the hint collection under user_id and user_id + kind, after the card and chunk indexes, with
the collection prefix applied."""
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


def test_two_hint_vector_indexes_last(tmp_path):
    calls = _calls(tmp_path)
    hint_calls = [c for c in calls if "--collection-group=development_drive_index_hints" in c]
    assert len(hint_calls) == 2 and calls[-2:] == hint_calls
    assert all("field-path=hint_vector" in c and '"dimension":"768"' in c for c in hint_calls)
    assert "field-path=kind" not in hint_calls[0] and "field-path=kind" in hint_calls[1]


def test_the_prefix_applies_to_the_hint_collection(tmp_path):
    calls = _calls(tmp_path, COLLECTION_PREFIX="test_")
    assert sum("--collection-group=test_drive_index_hints" in c for c in calls) == 2
