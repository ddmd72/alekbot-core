"""The scheduler script carries every job the handler's recovery paths rely on, and config/firestore.indexes.json is the
live (env-prefixed) database's index configuration."""
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "cloudbuild_scheduler_jobs.sh"
_INDEXES = _ROOT / "config" / "firestore.indexes.json"


def _job_blocks(job_name):
    """The `update` and the `create` branch of one job's describe && update || create statement."""
    script = _SCRIPT.read_text()
    start = script.index(f"gcloud scheduler jobs describe {job_name} ")
    end = script.find("\ngcloud scheduler jobs describe", start + 1)
    block = script[start:end if end != -1 else None]
    update, create = block.split("|| gcloud scheduler jobs create")
    return update, create


@pytest.mark.parametrize("job, task_type, schedule", [
    ("alek-bot-dev-email-indexing-watchdog", "email_indexing_watchdog", "27 */2 * * *"),
    ("alek-bot-dev-sweep-companion-consolidation", "sweep_companion_consolidation", "47 * * * *"),
])
def test_recovery_job_is_defined_with_its_task_type_schedule_and_oidc(job, task_type, schedule):
    for branch in _job_blocks(job):
        assert f"--schedule=\"{schedule}\"" in branch
        assert f"--message-body='{{\"task_type\":\"{task_type}\"}}'" in branch
        assert '--uri="$SERVICE_URL/worker"' in branch
        assert '--oidc-service-account-email="$SA"' in branch
        assert '--oidc-token-audience="$SERVICE_URL/worker"' in branch
        assert "--http-method=POST" in branch


def test_no_scheduler_job_is_created_twice():
    script = _SCRIPT.read_text()
    bodies = re.findall(r"jobs create http (\S+)", script)
    assert len(bodies) == len(set(bodies))


def _entries():
    return json.loads(_INDEXES.read_text())


def test_every_collection_group_carries_the_env_prefix():
    stray = sorted({e["collectionGroup"] for e in _entries() if not e["collectionGroup"].startswith("development_")})
    assert not stray, f"collection groups without the development_ prefix: {stray}"


def _fields(collection):
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "vector") for f in e["fields"]]
            for e in _entries() if e["collectionGroup"] == collection and "fields" in e]


def test_task_search_vector_indexes_are_declared():
    declared = _fields("development_task_search_index")
    for vector in ("content_vector", "context_vector"):
        assert [("user_id", "ASCENDING"), (vector, "vector")] in declared
        assert [("user_id", "ASCENDING"), ("status", "ASCENDING"), (vector, "vector")] in declared


def test_email_vector_indexes_use_the_live_field_order():
    declared = _fields("development_domain_email_facts_v1")
    vectors = [d for d in declared if d[-1][1] == "vector"]
    assert len(vectors) == 8
    assert all([n for n, _ in d[:2]] == ["state", "user_id"] for d in vectors)


def test_a_vector_entry_declares_dimension_768_flat():
    for e in _entries():
        for f in e.get("fields", []):
            if "vectorConfig" in f:
                assert f["vectorConfig"] == {"dimension": 768, "flat": {}}, e["collectionGroup"]


def test_the_unused_legacy_facts_collection_has_no_entries():
    assert not [e for e in _entries() if e["collectionGroup"] == "development_facts"]
