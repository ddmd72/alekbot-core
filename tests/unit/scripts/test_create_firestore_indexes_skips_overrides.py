"""The legacy create_firestore_indexes.sh reads the same index file (USER_DRIVE_INDEXING_RFC §12.5): a single-field
override entry has no `fields`, so it is skipped instead of becoming a composite create without fields."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "create_firestore_indexes.sh"


@pytest.mark.skipif(shutil.which("jq") is None, reason="the script needs jq")
def test_a_field_override_is_skipped_without_a_gcloud_call(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "firestore.indexes.json").write_text(
        json.dumps([{"collectionGroup": "chunks", "fieldPath": "text", "indexes": []}]))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(f"#!/bin/sh\necho called >> {tmp_path}/calls\n")
    (bin_dir / "gcloud").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "PROJECT_ID": "p"}
    run = subprocess.run(["bash", str(_SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert "Skipping a single-field override for chunks" in run.stdout
    assert not (tmp_path / "calls").exists()
