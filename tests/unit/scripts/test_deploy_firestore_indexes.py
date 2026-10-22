"""scripts/infrastructure/deploy_firestore_indexes.py (`make deploy-indexes`): array fields are created with
`array-config`, a vector entry is skipped whole (its own gcloud script creates it), and any gcloud failure other than
"already exists" makes the script exit non-zero. gcloud is never called: `run_command` is replaced, and the CLI test
puts a stub `gcloud` first on PATH."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.datafile

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure" / "deploy_firestore_indexes.py"

_INDEXES = [
    {"collectionGroup": "plain", "queryScope": "COLLECTION",
     "fields": [{"fieldPath": "a", "order": "ASCENDING"}, {"fieldPath": "b", "order": "DESCENDING"}]},
    {"collectionGroup": "names", "queryScope": "COLLECTION",
     "fields": [{"fieldPath": "a", "order": "ASCENDING"}, {"fieldPath": "tags", "arrayConfig": "CONTAINS"},
                {"fieldPath": "b", "order": "ASCENDING"}]},
    {"collectionGroup": "vectors", "queryScope": "COLLECTION",
     "fields": [{"fieldPath": "a", "order": "ASCENDING"}, {"fieldPath": "k", "order": "ASCENDING"},
                {"fieldPath": "v", "vectorConfig": {"dimension": 768, "flat": {}}}]},
]


@pytest.fixture
def index_file(tmp_path):
    path = tmp_path / "indexes.json"
    path.write_text(json.dumps(_INDEXES))
    return path


def _load(result):
    spec = importlib.util.spec_from_file_location("deploy_firestore_indexes", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands = []
    module.run_command = lambda cmd: commands.append(cmd) or result
    return module, commands


def test_array_fields_get_array_config_and_vector_entries_are_skipped_whole(index_file):
    module, commands = _load(SimpleNamespace(returncode=0, stderr=""))
    assert module.deploy_indexes(str(index_file), "p") == []
    assert [c[5] for c in commands] == ["--collection-group=plain", "--collection-group=names"]
    assert "--field-config=field-path=tags,array-config=contains" in commands[1]
    assert "--field-config=field-path=b,order=descending" in commands[0]


def test_failures_are_returned_and_already_exists_is_not_one(index_file):
    module, _ = _load(SimpleNamespace(returncode=1, stderr="ERROR: PERMISSION_DENIED"))
    assert module.deploy_indexes(str(index_file), "p") == ["plain", "names"]
    module, _ = _load(SimpleNamespace(returncode=1, stderr="ERROR: index already exists"))
    assert module.deploy_indexes(str(index_file), "p") == []


@pytest.mark.parametrize("gcloud_exit,stderr,expected", [(0, "", 0), (1, "PERMISSION_DENIED", 1),
                                                         (1, "ALREADY_EXISTS: index already exists", 0)])
def test_cli_exit_code(tmp_path, index_file, gcloud_exit, stderr, expected):
    stub = tmp_path / "bin" / "gcloud"
    stub.parent.mkdir()
    stub.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {gcloud_exit}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{stub.parent}{os.pathsep}{os.environ['PATH']}"}
    run = subprocess.run([sys.executable, str(_SCRIPT), str(index_file), "p"], env=env, capture_output=True,
                         text=True, timeout=60)
    assert run.returncode == expected, run.stdout + run.stderr
