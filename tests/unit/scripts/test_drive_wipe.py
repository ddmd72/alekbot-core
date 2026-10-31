"""drive_wipe.py (the slice-1b deploy: no migration, the user's drive data is wiped and rebuilt from zero): a dry run
counts and writes nothing; --apply deletes every drive document of the user, subcollections included, and nothing of
another user, the monthly usage or the credentials; a re-run finds nothing. On the evaluating Firestore fake, with the
index refusal of config/firestore.indexes.json on (the wipe's queries need no composite)."""
import copy
import importlib.util
from pathlib import Path

from tests.support.firestore_eval import EvalDb

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "infrastructure" / "drive_wipe.py"
P = "development_"


def _load():
    spec = importlib.util.spec_from_file_location("drive_wipe", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _names(script):
    return script.Names(records=f"{P}drive_index_records", chunks=f"{P}drive_index_chunks",
                        hints=f"{P}drive_index_hints", jobs=f"{P}drive_transcript_jobs",
                        windows=f"{P}drive_transcript_windows", batches=f"{P}drive_transcript_batches",
                        sync_state=f"{P}drive_sync_state", transcripts=f"{P}drive_transcripts",
                        parts=f"{P}drive_transcript_parts", roots=f"{P}drive_mirror_roots",
                        ledger=f"{P}drive_transcript_mirror", notes=f"{P}drive_notes", usage=f"{P}drive_model_usage",
                        connections=f"{P}drive_connections", settings=f"{P}drive_user_settings")


def _seed(db, n, user):
    def put(name, doc_id, **data):
        db.collection(name).docs[doc_id] = {"user_id": user, **data}

    put(n.records, f"{user}:item-1", item_id="item-1")
    put(n.records, f"conn-{user}:item-2", item_id="item-2", connection_id=f"conn-{user}")
    put(n.chunks, f"{user}:item-1:1:0")
    put(n.hints, f"{user}:item-1:1:0")
    put(n.jobs, f"{user}:item-1")
    put(f"{n.jobs}/{user}:item-1/{n.windows}", "0", index=0)
    put(n.batches, f"{user}:batches%2Fb1", state="done")
    put(n.sync_state, user)
    put(n.sync_state, f"conn-{user}", connection_id=f"conn-{user}")
    put(n.transcripts, f"{user}:QXH1:100", content_key="QXH1:100", parts=2)
    for i in range(2):
        put(f"{n.transcripts}/{user}:QXH1:100/{n.parts}", f"t-{i}", index=i)
    put(n.roots, f"{user}:drv-1", drive_id="drv-1")
    put(n.ledger, f"{user}:file:item-1", kind="file")
    put(n.notes, f"{user}:n1", match_keys=["r:item-1"])
    put(f"{n.notes}/{user}:n1/versions", "v1", version=1)
    put(n.usage, f"{user}:initial", spent_usd=12.5)
    put(n.usage, f"{user}:initial:microsoft:sub-1", spent_usd=3.0)
    put(n.usage, f"{user}:2026-10", transcribe_usd=3.0)
    put(n.connections, f"{user}:conn-{user}", connection_id=f"conn-{user}")
    put(n.settings, user, primary_connection_id=f"conn-{user}")
    put(f"{P}oauth_credentials", f"{user}:cred-1", kind="microsoft_onedrive", secret="sealed")


def _world():
    script = _load()
    db = EvalDb(indexes=_ROOT / "config" / "firestore.indexes.json")
    n = _names(script)
    _seed(db, n, "usr-1")
    _seed(db, n, "usr-2")
    return script, db, n


def _snapshot(db):
    return {name: copy.deepcopy(col.docs) for name, col in db.cols.items() if col.docs}


def _of(db, user):
    return sorted((name, doc_id) for name, col in db.cols.items() for doc_id, data in col.docs.items()
                  if data.get("user_id") == user)


async def test_a_dry_run_counts_per_collection_and_writes_nothing():
    script, db, n = _world()
    before = _snapshot(db)
    counts = await script.wipe(db, n, "usr-1", apply=False)
    assert _snapshot(db) == before
    assert counts[n.records] == 2 and counts[n.sync_state] == 2 and counts[f"{n.transcripts}/{n.parts}"] == 2
    assert counts[f"{n.jobs}/{n.windows}"] == 1 and counts[f"{n.notes}/versions"] == 1
    assert counts[f"{n.usage} (initial build)"] == 2 and counts[n.settings] == 1 and counts[n.connections] == 1
    lines = script.report(counts, apply=False)
    assert all(line.startswith("would delete") for line in lines) and any(n.records in line for line in lines)


async def test_apply_deletes_every_drive_document_of_the_user_and_nothing_else():
    script, db, n = _world()
    other = [(name, i) for name, i in _of(db, "usr-2")]
    await script.wipe(db, n, "usr-1", apply=True)
    assert _of(db, "usr-1") == [(f"{P}drive_model_usage", "usr-1:2026-10"), (f"{P}oauth_credentials", "usr-1:cred-1")]
    assert _of(db, "usr-2") == other
    assert db.collection(n.settings).docs.keys() == {"usr-2"}


async def test_a_rerun_finds_nothing():
    script, db, n = _world()
    await script.wipe(db, n, "usr-1", apply=True)
    counts = await script.wipe(db, n, "usr-1", apply=True)
    assert not any(counts.values())
    assert script.report(counts, apply=True) == ["nothing stored for this user"]


async def test_an_empty_user_id_is_refused():
    script, db, n = _world()
    try:
        await script.wipe(db, n, "", apply=True)
    except ValueError:
        return
    raise AssertionError("an empty user id must be refused")
