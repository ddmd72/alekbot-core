"""Provisioning of the Search v2 queries (USER_DRIVE_INDEXING_RFC §25.2.5, §12.5): every query a v2 repository method
sends has its composite (or vector) index in config/firestore.indexes.json under the live `development_` collections;
the heavy v2 fields are exempt from single-field indexing; the hint collection gets its two vector entries; the two
`content_terms` composites stay until acceptance (§25.8)."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.domain.drive_index import DriveIndexRecord

from tests.unit.adapters.firestore_drive_eval_fakes import (
    CHUNKS, HINTS, RECS, EvalDb, eval_transactions, make_eval_repo,
)
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[3]
LIVE = {RECS: "development_drive_index_records", CHUNKS: "development_drive_index_chunks",
        HINTS: "development_drive_index_hints"}
T = datetime(2026, 10, 13, tzinfo=timezone.utc)


def _raw():
    return json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())


def _declared(collection):
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "VECTOR") for f in idx["fields"]]
            for idx in _raw() if idx["collectionGroup"] == collection and "fields" in idx]


def _exempt(collection):
    return {idx["fieldPath"] for idx in _raw()
            if idx["collectionGroup"] == collection and "fields" not in idx and idx.get("indexes") == []}


def _required(ops):
    """(prefix, tail): equality, `in` and array filters (any order), then the orderings (the range field is the first
    one) or the vector field. A count over equality and `in` filters needs its prefix as one composite (the
    count_unread practice). None for an equality-only read (single-field indexes serve it)."""
    prefix = {(op[1], "CONTAINS" if op[2].startswith("array_contains") else "EQ")
              for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "in", "array_contains",
                                                               "array_contains_any")}
    ranged = [op[1] for op in ops if op[0] == "where" and op[2] in ("<", "<=", ">", ">=")]
    orders = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
    nearest = [op[1] for op in ops if op[0] == "find_nearest"]
    if nearest:
        return prefix, [(nearest[0]["vector_field"], "VECTOR")]
    if ranged:
        assert orders and orders[0][0] == ranged[0], ops   # Firestore: the range field orders first
    if orders:
        return prefix, orders
    if any(op[0] == "count" for op in ops) and len(prefix) > 1:
        return prefix, []
    return None


def _covered(required, index):
    prefix, tail = required
    head, rest = index[:len(index) - len(tail)], index[len(index) - len(tail):]
    return rest == tail and {(p, "CONTAINS" if m == "CONTAINS" else "EQ") for p, m in head} == prefix


@pytest.fixture
def run(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield db, make_eval_repo(db, mock_env_config)


async def test_every_v2_query_has_a_declared_index(run):
    db, repo = run
    await repo.find_by_about_keys(U_SCOPE, ["e:a"], 5)
    await repo.find_by_about_keys(U_SCOPE, ["e:a"], 5, kind="pdf")
    await repo.find_by_about_keys(U_SCOPE, ["e:a"], 5, date_from=T, date_to=T)
    await repo.find_by_about_keys(U_SCOPE, ["e:a"], 5, kind="pdf", date_to=T)
    await repo.upsert_records([DriveIndexRecord("u", "c", connection_id="u")])   # the cursor record exists: its page query runs
    await repo.find_by_about_keys(U_SCOPE, ["e:a"], 5, after="c")
    await repo.find_expiring(U_SCOPE, ["lv:a"], date(2026, 10, 13), 5)
    await repo.enrich_queue(U_SCOPE, 5)
    await repo.enrich_wakeups(U_SCOPE, T, 5)
    await repo.count_by_job_state(U_SCOPE, {"pdf", "docx"})
    await repo.keys_pending_page(U_SCOPE, None, 5)
    await repo.keys_pending_page(U_SCOPE, "a", 5)
    await repo.find_chunks_by_word_keys(U_SCOPE, ["w:a"], 5)
    await repo.find_chunks_by_word_keys(U_SCOPE, ["w:a"], 5, after_item_id="a")
    await repo.find_hints_by_keys(U_SCOPE, ["w:a"], 5)
    await repo.find_hints_by_keys(U_SCOPE, ["w:a"], 5, after_item_id="a")
    await repo.find_nearest_hints(U_SCOPE, [1.0], 5)
    await repo.find_nearest_hints(U_SCOPE, [1.0], 5, kind="pdf")
    await repo.get_hints(U_SCOPE, "a", 1)
    await repo.get_hints_with_vectors(U_SCOPE, "a", 1)
    await repo.hints_page(U_SCOPE, None, 5)
    await repo.hints_page(U_SCOPE, ("a", 1, 0), 5)
    await repo.delete_hints_except(U_SCOPE, "a", 1)
    await repo.delete_user_hints(U_SCOPE, 5)
    missing, needing = [], 0
    for name, live in LIVE.items():
        declared = _declared(live)
        for ops in db.cols[name].executed:
            required = _required(ops)
            if required is None:
                continue
            needing += 1
            if not any(_covered(required, i) for i in declared):
                missing.append((live, ops))
    assert missing == []
    # about 5, expiring, queue, wake-ups, 12 counts (2 jobs x 6 states), and two each of keys pending, words,
    # hint keys, hint kNN, hint generations, hint pages; the two deletes read by equality only
    assert needing == 5 + 1 + 1 + 1 + 12 + 2 + 2 + 2 + 2 + 2 + 2


def test_the_heavy_v2_fields_are_exempt_from_single_field_indexing():
    assert {"about_keys", "profile", "readme_excerpt", "place_forms", "photo_description", "photo_objects",
            "photo_scene", "visible_text"} <= _exempt("development_drive_index_records")
    assert "word_keys" in _exempt("development_drive_index_chunks")
    assert {"hint_keys", "items", "hint_vector"} <= _exempt("development_drive_index_hints")


def test_the_hint_vector_entries_and_no_kind_hint_keys_composite():
    hints = _declared("development_drive_index_hints")
    vectors = [i for i in hints if i[-1] == ("hint_vector", "VECTOR")]
    assert vectors == [[("connection_id", "ASCENDING"), ("hint_vector", "VECTOR")],
                       [("connection_id", "ASCENDING"), ("kind", "ASCENDING"), ("hint_vector", "VECTOR")],
                       [("connection_id", "ASCENDING"), ("item_id", "ASCENDING"), ("hint_vector", "VECTOR")]]   # Δ246
    assert not any(("hint_keys", "CONTAINS") in i and ("kind", "ASCENDING") in i for i in hints)


def test_the_content_terms_composites_stay_until_acceptance():
    chunks = _declared("development_drive_index_chunks")
    assert sum(("content_terms", "CONTAINS") in i for i in chunks) == 2


def test_vector_dimensions_are_the_embedding_size():
    for idx in _raw():
        if idx["collectionGroup"] == "development_drive_index_hints" and "fields" in idx:
            for f in idx["fields"]:
                if "vectorConfig" in f:
                    assert f["vectorConfig"] == {"dimension": 768, "flat": {}}
