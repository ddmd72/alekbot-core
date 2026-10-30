"""The cut-over script's production edges (MULTI_DRIVE_RFC §12 step 3): the READY check compares the slice's new
indexes of config/firestore.indexes.json with what gcloud lists; the queue and Scheduler reads parse gcloud's JSON (a
stub `gcloud` first on PATH); the connection store deletes by the product's document ids; a user with an old index and
no credential is purged and nothing is copied; `--user` limits the run."""
import json
import os

from src.domain.drive_connection import connection_doc_id
from tests.support.firestore_eval import EvalDb
from tests.unit.scripts.multi_drive_cutover_world import INDEXES, USER, load_script, names, world

_STUB = """#!/bin/bash
case "$1 $2 $3" in
  "tasks queues describe") echo '{"state": "'"$QUEUE_STATE"'"}' ;;
  "tasks list --queue=drive-sync-dev") echo "$TASKS" ;;
  "scheduler jobs describe") if [[ "$4" == "job-a" ]]; then echo '{"state": "PAUSED"}'; else echo '{"state": "ENABLED"}'; fi ;;
  "firestore indexes composite") cat "$STUB_DIR/listed.json" ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


def test_the_required_indexes_are_the_slices_twins():
    script = load_script()
    required = script.required_indexes(INDEXES)
    assert required and all(any(f["fieldPath"] in ("connection_id", "drive_identity") for f in i["fields"])
                            for i in required)
    groups = {i["collectionGroup"] for i in required}
    assert {"development_drive_index_records", "development_drive_index_chunks", "development_drive_index_hints",
            "development_drive_transcript_jobs", "development_drive_transcript_batches",
            "development_drive_transcript_windows", "development_drive_transcript_mirror"} == groups


def test_missing_from_compares_shapes_and_only_ready_ones_count():
    script = load_script()
    required = [{"collectionGroup": "c", "queryScope": "COLLECTION",
                 "fields": [{"fieldPath": "connection_id", "order": "ASCENDING"},
                            {"fieldPath": "vector", "vectorConfig": {"dimension": 768, "flat": {}}}]},
                {"collectionGroup": "c", "queryScope": "COLLECTION",
                 "fields": [{"fieldPath": "connection_id", "order": "ASCENDING"},
                            {"fieldPath": "item_id", "order": "ASCENDING"}]}]
    listed = [{"name": "projects/p/databases/d/collectionGroups/c/indexes/1", "queryScope": "COLLECTION",
               "state": "READY", "fields": [{"fieldPath": "connection_id", "order": "ASCENDING"},
                                            {"fieldPath": "vector", "vectorConfig": {"dimension": 768}},
                                            {"fieldPath": "__name__", "order": "ASCENDING"}]},
              {"name": "projects/p/databases/d/collectionGroups/c/indexes/2", "queryScope": "COLLECTION",
               "state": "CREATING", "fields": [{"fieldPath": "connection_id", "order": "ASCENDING"},
                                               {"fieldPath": "item_id", "order": "ASCENDING"}]}]
    assert script.missing_from(required, listed) == ["c (connection_id, item_id)"]


async def test_gcloud_infra_reads_the_queue_the_jobs_and_the_indexes(tmp_path, monkeypatch):
    script = load_script()
    (tmp_path / "gcloud").write_text(_STUB)
    (tmp_path / "gcloud").chmod(0o755)
    (tmp_path / "listed.json").write_text(json.dumps([]))
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("STUB_DIR", str(tmp_path))
    monkeypatch.setenv("QUEUE_STATE", "PAUSED")
    monkeypatch.setenv("TASKS", json.dumps([{"name": "t1"}, {"name": "t2"}]))
    infra = script.GcloudInfra(project="p", location="l", queue_name="drive-sync-dev", job_names=["job-a", "job-b"],
                               database="db")
    assert await infra.queue() == (True, 2)
    assert await infra.jobs_paused() == {"job-a": True, "job-b": False}
    required = script.required_indexes(INDEXES)
    assert len(await infra.missing_indexes(required)) == len(required)


async def test_the_repository_store_deletes_the_connection_and_settings_documents():
    script = load_script()
    w = await _applied_world()
    (conn,) = await w.store.list_for_user(USER)
    db = EvalDb()
    n = names(script)
    db.collection(n.connections).docs[connection_doc_id(conn.scope)] = {"user_id": USER}
    db.collection(n.settings).docs[USER] = {"user_id": USER}
    store = script.RepositoryConnectionStore(w.store.repo, db, n)
    await store.remove(conn)
    await store.remove_settings(USER)
    assert db.collection(n.connections).docs == {} and db.collection(n.settings).docs == {}
    assert [c.connection_id for c in await store.list_for_user(USER)] == [conn.connection_id]


async def _applied_world():
    w = world()
    code, lines = await w.go("apply")
    assert code == 0, lines
    return w


async def test_a_user_with_an_old_index_and_no_credential_is_purged_and_nothing_is_copied():
    w = world()
    n = w.names
    w.col(n.sync_state)["usr-2"] = {"user_id": "usr-2", "lease_owner": None, "lease_until": None}
    w.col(n.records)["usr-2:item-7"] = {"user_id": "usr-2", "item_id": "item-7"}
    w.col(n.transcripts)["usr-2:QXH7:7"] = {"user_id": "usr-2", "content_key": "QXH7:7"}
    code, lines = await w.go("apply")
    assert code == 0, lines
    assert w.doc(n.records, "usr-2:item-7") is None and w.doc(n.sync_state, "usr-2") is None
    assert w.doc(n.transcripts, "usr-2:QXH7:7") is not None and await w.store.list_for_user("usr-2") == []
    assert any(line.startswith("usr-2") and "nothing is copied" in line for line in lines)


async def test_user_limits_the_run():
    w = world()
    w.col(w.names.sync_state)["usr-2"] = {"user_id": "usr-2"}
    code, lines = await w.script.run_cutover(w.run(), "apply", write=True, only_users=["usr-2"])
    assert code == 0, lines
    assert await w.store.list_for_user(USER) == [] and w.doc(w.names.sync_state, USER) is not None
