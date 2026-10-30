"""Queues, wake-ups, copies in flight, orphans and the purge of the transcript documents (USER_DRIVE_INDEXING_RFC
§27.3-§27.5, §27.8 b, plan G12), on both adapters. Firestore-only cases pin the wire: the purge reaches the
subcollections (including the ones whose parent is gone), every new query has its declared index, and the heavy fields
are exempt from indexing."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_index_params import KEYED_DELETE_BATCH
from src.domain.drive_transcript import (
    Segment, StitchedTranscript, StoredTranscript, TranscriptBatch, TranscriptJob, WindowTranscript,
)

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, EvalDb, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_transcript_fakes import transcript_doc_fakes
from tests.unit.adapters.test_firestore_drive_index_v2_provisioning import _covered, _declared, _exempt, _required
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)
LIVE_RECS = "development_drive_index_records"
LIVE = {"windows": "development_drive_transcript_windows", "parts": "development_drive_transcript_parts",
        "jobs": "development_drive_transcript_jobs", "batches": "development_drive_transcript_batches",
        "store": "development_drive_transcripts"}
_ROOT = Path(__file__).resolve().parents[3]
# one connection per user here, its id the user's (MULTI_DRIVE_RFC §8): the document paths below read `{connection}:…`
V_SCOPE = ConnectionScope("v", "v")


def _scope(user):
    return ConnectionScope(user, user)


@pytest.fixture(params=["memory", "firestore"])
def adapter(request, mock_env_config):
    if request.param == "memory":
        yield InMemoryDriveIndexRepository(), None
        return
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = LIVE["jobs"]
    mock_env_config.drive_transcript_batches_collection = LIVE["batches"]
    mock_env_config.drive_transcripts_collection = LIVE["store"]
    mock_env_config.drive_transcript_windows_subcollection = LIVE["windows"]
    mock_env_config.drive_transcript_parts_subcollection = LIVE["parts"]
    with eval_transactions(), transcript_doc_fakes():
        yield make_eval_repo(db, mock_env_config), db


def _rec(item_id, user="u", **kw):
    return DriveIndexRecord(user, item_id, connection_id=user, **kw)


def _stored(key, last_used_at, text="x"):
    return StoredTranscript(key, StitchedTranscript((Segment(0.0, None, text),), ("uk",), 1, 5.0),
                            last_used_at=last_used_at)


def _result():
    return WindowTranscript(language_codes=("uk",), speakers=1, no_speech=False, segments=(Segment(0.0, None, "t"),))


# -- the record queues ------------------------------------------------------------------------------------------------

async def test_queue_order_newest_first(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        _rec("old", transcript_state="ready", effective_day="2026-01-01", parent_id="p"),
        _rec("new-b", transcript_state="ready", effective_day="2026-10-01", parent_id="p"),
        _rec("new-a", transcript_state="ready", effective_day="2026-10-01", parent_id="p"),
        _rec("new-0", transcript_state="ready", effective_day="2026-10-01", parent_id="a"),
        _rec("busy", transcript_state="working", effective_day="2026-12-01"),
        _rec("none", effective_day="2026-12-02"),
        _rec("other-user", user="v", transcript_state="ready", effective_day="2026-12-03"),
    ])
    assert [r.item_id for r in await repo.transcript_queue(U_SCOPE, 10)] == ["new-0", "new-a", "new-b", "old"]
    assert [r.item_id for r in await repo.transcript_queue(U_SCOPE, 2)] == ["new-0", "new-a"]


async def test_wakeups_due_only(adapter):
    repo, _ = adapter
    await repo.upsert_records([
        _rec("b", transcript_state="parked", transcript_wake_at=T),
        _rec("a", transcript_state="held", transcript_wake_at=T),
        _rec("early", transcript_state="held", transcript_wake_at=T - timedelta(hours=1)),
        _rec("later", transcript_state="held", transcript_wake_at=T + timedelta(seconds=1)),
        _rec("unset", transcript_state="ready"),
        _rec("other-user", user="v", transcript_wake_at=T - timedelta(days=1)),
    ])
    assert [r.item_id for r in await repo.transcript_wakeups(U_SCOPE, T, 10)] == ["early", "a", "b"]
    assert [r.item_id for r in await repo.transcript_wakeups(U_SCOPE, T, 2)] == ["early", "a"]


async def test_open_batches_by_next_poll(adapter):
    repo, _ = adapter
    for name, at in (("batches/c", T), ("batches/a", T - timedelta(minutes=9)), ("batches/b", T + timedelta(hours=1))):
        await repo.create_batch(TranscriptBatch(name=name, user_id="u", connection_id="u", next_poll_at=at))
    await repo.create_batch(TranscriptBatch(name="batches/z", user_id="v", connection_id="v", next_poll_at=T - timedelta(days=1)))
    await repo.create_batch(TranscriptBatch(name="batches/closed", user_id="u", connection_id="u", state="done", next_poll_at=T))
    assert [b.name for b in await repo.get_open_batches(U_SCOPE, T, 10)] == ["batches/a", "batches/c"]


# -- copies in flight -------------------------------------------------------------------------------------------------

async def test_jobs_by_content_key(adapter):
    repo, _ = adapter
    key = "sha/with:odd chars%"
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u", item_id="i2", content_key=key))
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u", item_id="i1", content_key=key))
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u", item_id="i3", content_key="other"))
    await repo.save_transcript_job(TranscriptJob(user_id="v", connection_id="v", item_id="i4", content_key=key))
    assert [j.item_id for j in await repo.jobs_by_content_key(U_SCOPE, key)] == ["i1", "i2"]
    assert await repo.jobs_by_content_key(U_SCOPE, "absent") == []
    assert await repo.jobs_by_content_key(_scope("w"), key) == []


# -- orphans ----------------------------------------------------------------------------------------------------------

async def test_orphans_by_last_used(adapter):
    repo, _ = adapter
    await repo.put_transcript("u", _stored("k/new", T))
    await repo.put_transcript("u", _stored("k-old", T - timedelta(days=40)))
    await repo.put_transcript("u", _stored("k-older", T - timedelta(days=90)))
    await repo.put_transcript("u", _stored("k-edge", T - timedelta(days=30)))
    await repo.put_transcript("v", _stored("k-other-user", T - timedelta(days=99)))
    cutoff = T - timedelta(days=30)
    assert await repo.orphan_transcripts("u", cutoff, 10) == ["k-older", "k-old"]
    assert await repo.orphan_transcripts("u", cutoff, 1) == ["k-older"]
    await repo.touch_transcript("u", "k-older", T)   # a used transcript is no longer an orphan
    assert await repo.orphan_transcripts("u", cutoff, 10) == ["k-old"]


# -- purge ------------------------------------------------------------------------------------------------------------

async def _fill(repo):
    for user in ("u", "v"):
        for i in (1, 2):
            await repo.save_transcript_job(TranscriptJob(user_id=user, connection_id=user, item_id=f"i{i}",
                                                         content_key=f"k{i}"))
            await repo.save_window_result(_scope(user), f"i{i}", 0, _result())
            await repo.save_window_result(_scope(user), f"i{i}", 1, _result())
            await repo.create_batch(TranscriptBatch(name=f"batches/{i}", user_id=user, connection_id=user,
                                                    next_poll_at=T))
            await repo.put_transcript(user, _stored(f"k{i}", T))


async def test_purge_helpers_delete_only_the_user(adapter):
    repo, db = adapter
    await _fill(repo)
    assert await repo.delete_user_transcript_jobs(U_SCOPE, 100) == 2
    assert await repo.delete_user_batches(U_SCOPE, 100) == 2
    assert await repo.delete_user_transcripts("u", 100) == 2
    for call, owner in ((repo.delete_user_transcript_jobs, U_SCOPE), (repo.delete_user_batches, U_SCOPE),
                        (repo.delete_user_transcripts, "u")):
        assert await call(owner, 100) == 0
    for i in (1, 2):
        assert await repo.get_transcript_job(U_SCOPE, f"i{i}") is None
        assert await repo.get_window_results(U_SCOPE, f"i{i}") == {}
        assert await repo.get_transcript("u", f"k{i}") is None
        assert len(await repo.get_window_results(V_SCOPE, f"i{i}")) == 2
        assert (await repo.get_transcript_job(V_SCOPE, f"i{i}")) is not None
        assert (await repo.get_transcript("v", f"k{i}")) is not None
    assert [b.name for b in await repo.get_open_batches(V_SCOPE, T, 10)] == ["batches/1", "batches/2"]
    if db is not None:   # the other user's subdocuments are all still there, the user's are gone
        assert len(db.collection(f"{LIVE['jobs']}/v:i1/{LIVE['windows']}").docs) == 2
        assert db.collection(f"{LIVE['jobs']}/u:i1/{LIVE['windows']}").docs == {}


async def test_purge_respects_the_limit_and_resumes(adapter):
    repo, _ = adapter
    await _fill(repo)
    assert await repo.delete_user_transcript_jobs(U_SCOPE, 1) == 1
    assert await repo.delete_user_transcript_jobs(U_SCOPE, 1) == 1
    assert await repo.delete_user_transcript_jobs(U_SCOPE, 1) == 0


@pytest.fixture
def fs(mock_env_config):
    db = EvalDb()
    mock_env_config.drive_transcript_jobs_collection = LIVE["jobs"]
    mock_env_config.drive_transcript_batches_collection = LIVE["batches"]
    mock_env_config.drive_transcripts_collection = LIVE["store"]
    mock_env_config.drive_transcript_windows_subcollection = LIVE["windows"]
    mock_env_config.drive_transcript_parts_subcollection = LIVE["parts"]
    with eval_transactions(), transcript_doc_fakes():
        yield db, make_eval_repo(db, mock_env_config)


async def _plant_orphans(db, repo, monkeypatch):
    from src.adapters import firestore_drive_index_repo as fs_module
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 600)
    big = StoredTranscript("k1", StitchedTranscript(tuple(Segment(i * 1.0, None, "w" * 80) for i in range(30)),
                                                    ("uk",), 1, 30.0), last_used_at=T)
    await repo.put_transcript("u", big)
    parts = db.collection(f"{LIVE['store']}/u:k1/{LIVE['parts']}")
    assert len(parts.docs) > 1
    # a leftover of a put that never wrote its head, and parts/windows whose parent document is gone
    parts.docs["deadbeef-0"] = {"user_id": "u", "index": 0, "token": "deadbeef", "segments": []}
    db.collection(f"{LIVE['store']}/u:gone/{LIVE['parts']}").docs["t-0"] = {"user_id": "u", "index": 0, "token": "t"}
    db.collection(f"{LIVE['store']}/v:gone/{LIVE['parts']}").docs["t-0"] = {"user_id": "v", "index": 0, "token": "t"}
    db.collection(f"{LIVE['jobs']}/u:gone/{LIVE['windows']}").docs["0"] = {"user_id": "u", "connection_id": "u",
                                                                          "index": 0, "segments": []}
    db.collection(f"{LIVE['jobs']}/v:gone/{LIVE['windows']}").docs["0"] = {"user_id": "v", "connection_id": "v",
                                                                          "index": 0, "segments": []}
    return parts


async def test_helpers_reach_parts_of_any_token_and_never_use_a_collection_group(fs, monkeypatch):
    db, repo = fs
    parts = await _plant_orphans(db, repo, monkeypatch)
    await repo.save_transcript_job(TranscriptJob(user_id="u", connection_id="u", item_id="i1", content_key="k1"))
    await repo.save_window_result(U_SCOPE, "i1", 0, _result())
    assert await repo.delete_user_transcripts("u", 100) == 1
    assert parts.docs == {}
    assert await repo.delete_user_transcript_jobs(U_SCOPE, 100) == 1
    assert db.collection(f"{LIVE['jobs']}/u:i1/{LIVE['windows']}").docs == {}
    assert await repo.delete_user_transcripts("u", 100) == 0
    assert getattr(db, "group_executed", {}) == {}   # no collection-group query was sent
    # the parentless subdocuments are still there: only the orphan method reaches them
    assert len(db.collection(f"{LIVE['store']}/u:gone/{LIVE['parts']}").docs) == 1
    assert len(db.collection(f"{LIVE['jobs']}/u:gone/{LIVE['windows']}").docs) == 1


async def test_orphan_sweep_deletes_only_the_users_parentless_subdocs(fs, monkeypatch):
    db, repo = fs
    await _plant_orphans(db, repo, monkeypatch)
    await repo.delete_user_transcripts("u", 100)
    # a same-named group that is not ours (bare name) is not in the prefixed group
    db.collection("somewhere_else/u:x/parts").docs["0"] = {"user_id": "u", "index": 0}
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 100, keep_transcripts=False) == 2
    assert db.collection(f"{LIVE['store']}/u:gone/{LIVE['parts']}").docs == {}
    assert db.collection(f"{LIVE['jobs']}/u:gone/{LIVE['windows']}").docs == {}
    assert len(db.collection(f"{LIVE['store']}/v:gone/{LIVE['parts']}").docs) == 1
    assert len(db.collection(f"{LIVE['jobs']}/v:gone/{LIVE['windows']}").docs) == 1
    assert len(db.collection("somewhere_else/u:x/parts").docs) == 1
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 100, keep_transcripts=False) == 0


async def test_orphan_sweep_respects_the_limit(fs, monkeypatch):
    db, repo = fs
    await _plant_orphans(db, repo, monkeypatch)
    await repo.delete_user_transcripts("u", 100)
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 1, keep_transcripts=False) == 1
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 1, keep_transcripts=False) == 1
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 1, keep_transcripts=False) == 0


async def test_orphan_windows_are_swept_on_both_adapters(adapter):
    repo, _ = adapter
    await repo.save_window_result(U_SCOPE, "gone", 0, _result())   # its job is gone (the helpers already returned 0)
    await repo.save_window_result(V_SCOPE, "gone", 0, _result())
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 10, keep_transcripts=False) == 1
    assert await repo.get_window_results(U_SCOPE, "gone") == {}
    assert len(await repo.get_window_results(V_SCOPE, "gone")) == 1


async def test_a_failing_group_query_fails_only_the_orphan_method(fs):
    from google.api_core import exceptions as gexc
    from src.domain.drive_index import DriveIndexStoreUnavailable
    db, repo = fs
    await _fill(repo)
    for group in (LIVE["windows"], LIVE["parts"]):
        db.collection(f"{LIVE['jobs']}/gone/{group}")   # the group exists

    async def boom(self):
        raise gexc.FailedPrecondition("requires an index")
    from tests.unit.adapters import firestore_drive_transcript_fakes as fakes
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fakes._GroupQuery, "get", boom)
        assert await repo.delete_user_transcript_jobs(U_SCOPE, 100) == 2
        assert await repo.delete_user_batches(U_SCOPE, 100) == 2
        assert await repo.delete_user_transcripts("u", 100) == 2
        with pytest.raises(DriveIndexStoreUnavailable):
            await repo.delete_user_transcript_orphans(U_SCOPE, 100, keep_transcripts=False)


async def test_purge_deletes_in_keyed_batches(fs):
    db, repo = fs
    n = 2 * KEYED_DELETE_BATCH + 3
    for i in range(n):
        await repo.create_batch(TranscriptBatch(name=f"batches/{i}", user_id="u", connection_id="u"))
    db.commits.clear()
    assert await repo.delete_user_batches(U_SCOPE, n) == n
    assert [len(c) for c in db.commits] == [KEYED_DELETE_BATCH, KEYED_DELETE_BATCH, 3]


async def test_purge_queries_filter_on_the_user_only(fs):
    db, repo = fs
    await _fill(repo)
    for col in db.cols.values():
        col.executed.clear()
    db.group_executed = {}
    await repo.delete_user_transcript_jobs(U_SCOPE, 100)
    await repo.delete_user_batches(U_SCOPE, 100)
    await repo.delete_user_transcripts("u", 100)
    top = [LIVE["jobs"], LIVE["batches"], LIVE["store"]]
    for name in top:
        filters = [op for ops in db.cols[name].executed for op in ops if op[0] == "where"]
        assert filters and {(op[1], op[2]) for op in filters} == {("user_id", "==")}
    for ops in (o for runs in db.group_executed.values() for o in runs):
        assert {(op[1], op[2]) for op in ops if op[0] == "where"} == {("user_id", "==")}


# -- provisioning -----------------------------------------------------------------------------------------------------

async def test_every_new_query_has_its_index(fs):
    db, repo = fs
    await repo.upsert_records([_rec("c")])   # the records collection exists
    await repo.transcript_queue(U_SCOPE, 5)
    await repo.transcript_wakeups(U_SCOPE, T, 5)
    await repo.jobs_by_content_key(U_SCOPE, "k")
    await repo.get_open_batches(U_SCOPE, T, 5)
    await repo.orphan_transcripts("u", T, 5)
    await repo.delete_user_transcript_jobs(U_SCOPE, 5)
    await repo.delete_user_batches(U_SCOPE, 5)
    await repo.delete_user_transcripts("u", 5)
    executed = {LIVE_RECS: db.cols[RECS].executed, **{LIVE[k]: db.cols[LIVE[k]].executed for k in ("jobs", "batches", "store")}}
    needing = 0
    for collection, runs in executed.items():
        for ops in runs:
            required = _required(ops)
            if required is None:
                continue
            needing += 1
            assert any(_covered(required, i) for i in _declared(collection)), (collection, ops)
    # queue, wake-ups, open batches, orphans
    assert needing == 4
    # the equality-only copy check still has its composite (the brief's), and the sweep's group reads theirs
    assert [("user_id", "ASCENDING"), ("content_key", "ASCENDING")] in _declared(LIVE["jobs"])
    await repo.delete_user_transcript_orphans(U_SCOPE, 5, keep_transcripts=False)
    for group in (LIVE["windows"], LIVE["parts"]):
        (ops,) = db.group_executed[group][:1]
        required = _required(ops)
        assert required == ({("user_id", "EQ")}, [("index", "ASCENDING")])
        groups = [i for i in _raw() if i["collectionGroup"] == group and i.get("queryScope") == "COLLECTION_GROUP"]
        assert [[(f["fieldPath"], f["order"]) for f in i["fields"]] for i in groups] == [
            [("user_id", "ASCENDING"), ("index", "ASCENDING")]]


def _raw():
    return json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())


def test_the_declared_queue_indexes_match_the_brief():
    assert _declared(LIVE_RECS).count([("user_id", "ASCENDING"), ("transcript_state", "ASCENDING"),
                                       ("effective_day", "DESCENDING"), ("parent_id", "ASCENDING"),
                                       ("item_id", "ASCENDING")]) == 1
    assert _declared(LIVE_RECS).count([("user_id", "ASCENDING"), ("transcript_wake_at", "ASCENDING"),
                                       ("item_id", "ASCENDING")]) == 1
    assert _declared(LIVE["batches"]) == [[("user_id", "ASCENDING"), ("next_poll_at", "ASCENDING")]]
    assert _declared(LIVE["store"]) == [[("user_id", "ASCENDING"), ("last_used_at", "ASCENDING")]]


def test_the_heavy_transcript_fields_are_exempt_from_single_field_indexing():
    assert {"segments", "summary"} <= _exempt(LIVE["store"])
    assert "segments" in _exempt(LIVE["parts"]) and "segments" in _exempt(LIVE["windows"])


def test_the_deploy_tool_sends_gcloud_its_collection_group_scope(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("deploy_idx", _ROOT / "scripts" / "infrastructure" /
                                                  "deploy_firestore_indexes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sent = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(module, "run_command", lambda cmd: sent.append(cmd) or Done())
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump([i for i in _raw() if i.get("collectionGroup") == LIVE["windows"] and "fields" in i], f)
        path = f.name
    module.deploy_indexes(path, "p")
    assert sent and "--query-scope=collection-group" in sent[0]


QUEUE_METHODS = ("transcript_queue", "transcript_wakeups", "jobs_by_content_key", "orphan_transcripts",
                 "delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts",
                 "delete_user_transcript_orphans")


def test_every_new_method_is_wrapped_by_store_errors():
    import inspect
    from src.adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
    for name in QUEUE_METHODS:
        method = getattr(FirestoreDriveIndexRepository, name)
        assert inspect.iscoroutinefunction(method) and hasattr(method, "__wrapped__"), name


async def test_a_missing_queue_index_is_the_systemic_outage(fs):
    from google.api_core import exceptions as gexc
    from src.domain.drive_index import DriveIndexStoreUnavailable
    db, repo = fs
    db.collection(RECS).fail = gexc.FailedPrecondition("requires an index")
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.transcript_queue(U_SCOPE, 5)


async def test_a_failing_helper_read_is_the_outage(fs):
    from google.api_core import exceptions as gexc
    from src.domain.drive_index import DriveIndexStoreUnavailable
    db, repo = fs
    db.collection(LIVE["store"]).fail = gexc.FailedPrecondition("requires an index")
    with pytest.raises(DriveIndexStoreUnavailable):
        await repo.delete_user_transcripts("u", 5)   # a plain helper read failing is still the outage


async def test_keep_transcripts_leaves_a_sharded_transcript_readable(adapter, monkeypatch):
    repo, db = adapter
    from src.adapters import firestore_drive_index_repo as fs_module
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 600)
    big = StoredTranscript("k1", StitchedTranscript(tuple(Segment(i * 1.0, None, "w" * 80) for i in range(30)),
                                                    ("uk",), 1, 30.0), last_used_at=T)
    await repo.put_transcript("u", big)
    await repo.save_window_result(U_SCOPE, "i1", 0, _result())
    assert db is None or len(db.collection(f"{LIVE['store']}/u:k1/{LIVE['parts']}").docs) > 1
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 100, keep_transcripts=True) == 1   # the window only
    assert await repo.get_window_results(U_SCOPE, "i1") == {}
    assert await repo.get_transcript("u", "k1") == big
    if db is not None:
        assert db.group_executed.keys() == {LIVE["windows"]}   # the parts group was never queried


async def test_without_keep_transcripts_the_parts_go_too(adapter, monkeypatch):
    repo, db = adapter
    if db is None:
        pytest.skip("parts are separate documents on Firestore only")
    from src.adapters import firestore_drive_index_repo as fs_module
    monkeypatch.setattr(fs_module, "TRANSCRIPT_DOC_BYTES", 600)
    big = StoredTranscript("k1", StitchedTranscript(tuple(Segment(i * 1.0, None, "w" * 80) for i in range(30)),
                                                    ("uk",), 1, 30.0), last_used_at=T)
    await repo.put_transcript("u", big)
    count = len(db.collection(f"{LIVE['store']}/u:k1/{LIVE['parts']}").docs)
    assert await repo.delete_user_transcript_orphans(U_SCOPE, 100, keep_transcripts=False) == count
    assert db.collection(f"{LIVE['store']}/u:k1/{LIVE['parts']}").docs == {}


async def test_keep_transcripts_is_a_required_keyword(adapter):
    repo, _ = adapter
    with pytest.raises(TypeError):
        await repo.delete_user_transcript_orphans(U_SCOPE, 5)
    with pytest.raises(TypeError):
        await repo.delete_user_transcript_orphans(U_SCOPE, 5, True)
