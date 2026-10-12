"""Provisioning of the drive index (USER_DRIVE_INDEXING_RFC §12.5): every query FirestoreDriveIndexRepository
issues has its composite index in config/firestore.indexes.json, and `make deploy-indexes` creates them right."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.adapters.firestore_drive_fakes import NOW, RECS, Db, executed, make_repo, rec_doc, snap

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
            for idx in raw if idx["collectionGroup"] == collection]


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
    await repo.due_records("u", NOW, 10)
    await repo.oldest_due_at("u", 0)
    await repo.children("u", "p", "a", 5)
    await repo.records_page("u", "a", 5)
    await repo.records_seen_before("u", 2, 5)
    await repo.card_capped("u", 5)
    await repo.find_by_name_keys("u", ["w:a"], 5)
    await repo.find_by_name_keys("u", ["w:a"], 5, kind="pdf")
    await repo.filter_records("u", None, None, None, 8)
    await repo.filter_records("u", "pdf", NOW, NOW, 8)
    await repo.find_nearest_cards("u", [1.0], 5)
    await repo.find_nearest_cards("u", [1.0], 5, kind="pdf")
    await repo.count_by_state("u")
    await repo.delete_user_records("u", 5)
    db.cols[RECS].respond = lambda ops: ([snap(rec_doc("x", card_rank=2))]
                                         if ("order_by", "card_rank", "DESCENDING") in ops else [])
    await repo.lowest_ranked_carded("u")   # top rank, dated (empty), undated
    queries = executed(db)
    assert len(queries) == 27   # due_records 5, count_by_state 7, lowest_ranked_carded 3, the rest 1 each
    declared = _declared_indexes()
    missing = [ops for ops in queries
               if (req := _required_index(ops)) is not None and not any(_covered(req, i) for i in declared)]
    assert missing == []


def test_indexes_are_declared_for_the_live_collection_only():
    assert len(_declared_indexes()) == 13
    assert _declared_indexes("drive_index_records") == []   # plan Δ22: the unprefixed collections were deleted


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
    drive = [c for c in _deploy_commands() if "--collection-group=development_drive_index_records" in c]
    declared = [i for i in _declared_indexes() if not any(mode == "VECTOR" for _, mode in i)]
    assert len(drive) == len(declared) == 11   # the 2 vector entries: create_drive_vector_indexes.sh
    name_stream = [c for c in drive if "--field-config=field-path=name_tokens,array-config=contains" in c]
    assert len(name_stream) == 2
