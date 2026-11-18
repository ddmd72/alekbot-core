"""The index scripts take the database explicitly (TEST_COVERAGE_SYSTEMIC_RFC §4 "Indexes"): no live default. Without a
database each script exits non-zero before gcloud is called; with one, it is the database gcloud receives. gcloud is a
stub on PATH that records its calls."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_INFRA = Path(__file__).resolve().parents[3] / "scripts" / "infrastructure"
_SHELL_SCRIPTS = [
    "create_drive_vector_indexes.sh",
    "create_task_search_vector_indexes.sh",
    "create_drive_note_vector_index.sh",
    "create_drive_scoped_vector_indexes.sh",
    "create_email_card_vector_index.sh",
]
_DEPLOY = _INFRA / "deploy_firestore_indexes.py"
_DB = "some-invented-db"


def _stub(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(f'#!/bin/sh\necho "$*" >> "{tmp_path}/calls"\nexit 0\n')
    gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _calls(tmp_path):
    f = tmp_path / "calls"
    return f.read_text().splitlines() if f.exists() else []


def _env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("FIRESTORE_DATABASE", "APPLY")}
    env.update(PATH=f"{_stub(tmp_path)}{os.pathsep}{os.environ['PATH']}", PROJECT_ID="p", **extra)
    return env


@pytest.mark.parametrize("name", _SHELL_SCRIPTS)
def test_a_shell_script_without_a_database_fails_and_calls_nothing(tmp_path, name):
    proc = subprocess.run(["bash", str(_INFRA / name)], env=_env(tmp_path), capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "FIRESTORE_DATABASE" in proc.stderr
    assert _calls(tmp_path) == []


@pytest.mark.parametrize("name", _SHELL_SCRIPTS)
def test_a_shell_script_with_a_database_passes_it_to_gcloud(tmp_path, name):
    proc = subprocess.run(["bash", str(_INFRA / name)], env=_env(tmp_path, FIRESTORE_DATABASE=_DB, APPLY="1"),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    calls = _calls(tmp_path)
    assert calls and all(f"--database={_DB}" in c for c in calls)


def test_deploy_without_a_database_prints_usage_and_calls_nothing(tmp_path):
    indexes = tmp_path / "indexes.json"
    indexes.write_text(json.dumps([{"collectionGroup": "c", "fields": [{"fieldPath": "a", "order": "ASCENDING"}]}]))
    proc = subprocess.run([sys.executable, str(_DEPLOY), str(indexes), "p"], env=_env(tmp_path), capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode != 0
    assert "Usage" in proc.stdout and "<database_id>" in proc.stdout
    assert _calls(tmp_path) == []


def test_deploy_with_a_database_uses_it(tmp_path):
    indexes = tmp_path / "indexes.json"
    indexes.write_text(json.dumps([{"collectionGroup": "c", "fields": [{"fieldPath": "a", "order": "ASCENDING"}]}]))
    proc = subprocess.run([sys.executable, str(_DEPLOY), str(indexes), "p", _DB], env=_env(tmp_path),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1 and f"--database={_DB}" in calls[0]


def test_the_test_gate_setup_grants_the_bucket_read_role_beside_object_admin():
    text = (_INFRA / "setup_test_gate.sh").read_text()
    assert "--role=roles/storage.objectAdmin" in text
    assert "--role=roles/storage.legacyBucketReader" in text
