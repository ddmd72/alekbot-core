"""Provisioning of the drive index (USER_DRIVE_INDEXING_RFC §12.5): every query FirestoreDriveIndexRepository
issues has its composite index in config/firestore.indexes.json, and `make deploy-indexes` creates them right."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.adapters.firestore_drive_fakes import NOW, RECS, Db, executed, make_repo, rec_doc, snap
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def db():
    return Db()


@pytest.fixture
def repo(db, mock_env_config):
    return make_repo(db, mock_env_config)


def _declared_indexes(collection="development_drive_index_records"):
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "VECTOR") for f in idx["fields"]]
            for idx in raw if idx["collectionGroup"] == collection and "fields" in idx]   # not the exemptions


def _required_index(ops):
    """Equality and array filters first (any order), then the orderings in order (or the vector field). None when
    Firestore's automatic single-field indexes serve the query (equality filters only, no ordering)."""
    prefix = {(op[1], "CONTAINS" if op[2] == "array_contains_any" else "EQ")
              for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "array_contains_any")}
    nearest = [op[1] for op in ops if op[0] == "find_nearest"]
    if nearest:
        tail = [(nearest[0]["vector_field"], "VECTOR")]
    else:
        tail = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
    return (prefix, tail) if tail else None


def _covered(required, index):
    prefix, tail = required
    head, rest = index[:len(index) - len(tail)], index[len(index) - len(tail):]
    return rest == tail and {(path, "CONTAINS" if mode == "CONTAINS" else "EQ") for path, mode in head} == prefix


async def test_every_record_query_has_a_declared_index(repo, db):
    await repo.due_records(U_SCOPE, NOW, 10)
    await repo.oldest_due_at(U_SCOPE, 0)
    await repo.children(U_SCOPE, "p", "a", 5)
    await repo.records_page(U_SCOPE, "a", 5)
    await repo.records_seen_before(U_SCOPE, 2, 5)
    await repo.card_capped(U_SCOPE, 5)
    await repo.find_by_name_keys(U_SCOPE, ["w:a"], 5)
    await repo.find_by_name_keys(U_SCOPE, ["w:a"], 5, kind="pdf")
    await repo.filter_records(U_SCOPE, None, None, None, 8)
    await repo.filter_records(U_SCOPE, "pdf", NOW, NOW, 8)
    await repo.find_nearest_cards(U_SCOPE, [1.0], 5)
    await repo.find_nearest_cards(U_SCOPE, [1.0], 5, kind="pdf")
    await repo.count_by_state(U_SCOPE)
    await repo.delete_user_records(U_SCOPE, 5)
    db.cols[RECS].respond = lambda ops: ([snap(rec_doc("x", card_rank=2))]
                                         if ("order_by", "card_rank", "DESCENDING") in ops else [])
    await repo.lowest_ranked_carded(U_SCOPE)   # top rank, dated (empty), undated
    queries = executed(db)
    assert len(queries) == 27   # due_records 5, count_by_state 7, lowest_ranked_carded 3, the rest 1 each
    declared = _declared_indexes()
    missing = [ops for ops in queries
               if (req := _required_index(ops)) is not None and not any(_covered(req, i) for i in declared)]
    assert missing == []


def test_indexes_are_declared_for_the_live_collection_only():
    # 21 composites (incl. the readable-chunk sum's, §24.6; chunk_capped's and count_unread's three, §5.4, §10.2;
    # count_unread's three for images triaged as text, review D-M4; the not-read-yet count's two, documents and text
    # images, final review F5-2) + 8 Search v2 composites (§25.2.5: about, about by kind, expiring, enrichment queue,
    # wake-ups, profile and passages state counts, keys pending) + 2 per-job enrichment queues (P5 review MINOR 6)
    # + 1 for the records the bot cannot open (user_id + inaccessible_since, §26.2, §26.9 step 3) + 2 transcript
    # queues (the ready queue and the wake-ups, §27.3) + 2 transcript mirror queues (the mirror queue and its wake-ups,
    # §27.15 step 8c) + 2 vector entries
    assert len(_declared_indexes()) == 38
    assert _declared_indexes().count([("user_id", "ASCENDING"), ("transcript_state", "ASCENDING"),
                                      ("effective_day", "DESCENDING"), ("parent_id", "ASCENDING"),
                                      ("item_id", "ASCENDING")]) == 1
    assert _declared_indexes().count([("user_id", "ASCENDING"), ("transcript_wake_at", "ASCENDING"),
                                      ("item_id", "ASCENDING")]) == 1
    assert _declared_indexes().count([("user_id", "ASCENDING"), ("mirror_due", "ASCENDING"),
                                      ("effective_day", "DESCENDING"), ("item_id", "ASCENDING")]) == 1
    assert _declared_indexes().count([("user_id", "ASCENDING"), ("mirror_retry_at", "ASCENDING"),
                                      ("item_id", "ASCENDING")]) == 1
    assert _declared_indexes("drive_index_records") == []   # RFC Δ22: the unprefixed collections were deleted


async def test_the_not_read_yet_counts_have_their_composites(repo, db):
    """count_unread(pending=True) adds `card_capped == False` to the pending class (final review F5-2): one count
    per kind list, documents and text images, each served by a composite of exactly its equality fields."""
    await repo.count_unread(U_SCOPE, {"pdf", "docx"}, {"jpg"}, pending=True)
    queries = executed(db)
    assert len(queries) == 2
    declared = _declared_indexes()
    for ops in queries:
        assert ("where", "card_capped", "==", False) in ops, ops
        fields = {op[1] for op in ops if op[0] == "where"}
        assert any({path for path, _ in i} == fields for i in declared), ops


def _deploy_commands():
    """`make deploy-indexes` (scripts/infrastructure/deploy_firestore_indexes.py) run against the real file,
    with the gcloud call captured."""
    spec = importlib.util.spec_from_file_location(
        "deploy_firestore_indexes", _ROOT / "scripts" / "infrastructure" / "deploy_firestore_indexes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    commands = []
    module.run_command = lambda cmd: commands.append(cmd) or SimpleNamespace(returncode=0, stderr="")
    module.deploy_indexes(str(_ROOT / "config" / "firestore.indexes.json"), "p")
    return commands


def test_deploy_creates_the_drive_composites_and_leaves_the_vectors_to_their_script():
    records = [c for c in _deploy_commands() if "--collection-group=development_drive_index_records" in c]
    drive = [c for c in records if c[3] == "composite"]   # the v2 single-field exemptions are `fields update` (§25.2.5)
    assert len(records) == len(drive) + 8 and all(c[3] == "fields" and "--disable-indexes" in c
                                                  for c in records if c not in drive)
    declared = [i for i in _declared_indexes() if not any(mode == "VECTOR" for _, mode in i)]
    # +1 readable_chunk_total's sum, +1 chunk_capped, +3 count_unread, +3 count_unread over text images (review D-M4),
    # +2 the not-read-yet count over documents and text images (F5-2), +8 Search v2 (§25.2.5), +2 per-job enrichment
    # queues (P5 review MINOR 6), +1 the records the bot cannot open (§26.2, §26.9 step 3), +2 the transcript queue
    # and wake-ups (§27.3), +2 the transcript mirror queue and its wake-ups (§27.15 step 8c); the 2 vector entries:
    # create_drive_vector_indexes.sh
    assert len(drive) == len(declared) == 36
    mirror_queue = [c for c in drive if "--field-config=field-path=mirror_due,order=ascending" in c]
    assert len(mirror_queue) == 1 and mirror_queue[0][-4:] == [
        "--field-config=field-path=user_id,order=ascending",
        "--field-config=field-path=mirror_due,order=ascending",
        "--field-config=field-path=effective_day,order=descending",
        "--field-config=field-path=item_id,order=ascending"]
    mirror_wakeups = [c for c in drive if "--field-config=field-path=mirror_retry_at,order=ascending" in c]
    assert len(mirror_wakeups) == 1 and mirror_wakeups[0][-3:] == [
        "--field-config=field-path=user_id,order=ascending",
        "--field-config=field-path=mirror_retry_at,order=ascending",
        "--field-config=field-path=item_id,order=ascending"]
    transcript_queue = [c for c in drive if "--field-config=field-path=transcript_state,order=ascending" in c]
    assert len(transcript_queue) == 1 and transcript_queue[0][-5:] == [
        "--field-config=field-path=user_id,order=ascending",
        "--field-config=field-path=transcript_state,order=ascending",
        "--field-config=field-path=effective_day,order=descending",
        "--field-config=field-path=parent_id,order=ascending",
        "--field-config=field-path=item_id,order=ascending"]
    wakeups = [c for c in drive if "--field-config=field-path=transcript_wake_at,order=ascending" in c]
    assert len(wakeups) == 1 and wakeups[0][-3:] == [
        "--field-config=field-path=user_id,order=ascending",
        "--field-config=field-path=transcript_wake_at,order=ascending",
        "--field-config=field-path=item_id,order=ascending"]
    inaccessible = [c for c in drive if "--field-config=field-path=inaccessible_since,order=ascending" in c]
    assert len(inaccessible) == 1 and "--field-config=field-path=user_id,order=ascending" in inaccessible[0]
    not_read_yet = [c for c in drive if "--field-config=field-path=card_capped,order=ascending" in c
                    and "--field-config=field-path=indexed_generation,order=ascending" in c]
    assert len(not_read_yet) == 2
    assert sum("--field-config=field-path=triage_verdict,order=ascending" in c for c in not_read_yet) == 1
    readable_sum = [c for c in drive if "--field-config=field-path=chunk_count,order=ascending" in c]
    assert len(readable_sum) == 1
    name_stream = [c for c in drive if "--field-config=field-path=name_tokens,array-config=contains" in c]
    assert len(name_stream) == 2
