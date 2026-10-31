"""Provisioning of the transcript mirror's queries (USER_DRIVE_INDEXING_RFC §27.15 step 8c, Δ206, Δ217, Δ237): every
query a mirror repository method sends has its composite index declared in config/firestore.indexes.json under the live
`development_` collections, except `pinned_keys`, which needs none (equality filters and the document id only, so the
single-field indexes serve it and a privacy purge never waits for a mirror index, Δ237); the drive-scoped ones carry
`drive_identity` (MULTI_DRIVE_RFC §10), the pins and the deleted-files search span drives, and `archive_keys` is exempt
from single-field indexing. The record queries of the mirror step filter the connection (`connection_id`, §8)."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from src.domain.drive_mirror import MIRROR_ARCHIVED, MirrorDoc
from src.ports.drive_mirror_repository import ARCHIVE_FAMILY, NAME_FAMILY

from tests.unit.adapters.firestore_drive_eval_fakes import RECS, eval_transactions, make_eval_repo
from tests.unit.adapters.firestore_drive_mirror_fakes import LEDGER, ROOTS, NameAwareDb
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[2]
LIVE_LEDGER = "development_drive_transcript_mirror"
LIVE_RECS = "development_drive_index_records"
T = datetime(2026, 10, 21, tzinfo=timezone.utc)


def _declared(collection):
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig")) for f in idx["fields"]]
            for idx in raw if idx["collectionGroup"] == collection and "fields" in idx]


def _exempt(collection):
    raw = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    return {idx["fieldPath"] for idx in raw
            if idx["collectionGroup"] == collection and "fields" not in idx and idx.get("indexes") == []}


def _shape(ops):
    """(filter fields in any order, ordering) of a query: what the composite must list, the orderings last."""
    filters = {op[1] for op in ops if op[0] == "where"}
    orders = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
    return filters, orders


def _served(ops, declared):
    filters, orders = _shape(ops)
    tail = [(n, d if d in ("ASCENDING", "DESCENDING") else "ASCENDING") for n, d in orders]
    wanted_head = filters - {n for n, _ in tail}
    for index in declared:
        names = [n for n, _ in index]
        head, rest = index[:len(index) - len(tail)], index[len(index) - len(tail):]
        if tail and rest != tail:
            continue
        if {n for n, _ in head} == wanted_head and (tail or len(head) == len(index)):
            return True
        if not tail and set(names) == filters:
            return True
    return False


def _single_field_served(ops):
    """Equality filters (`==`, `in`) on fields, at most a range on the document id, no ordering: Firestore merges the
    single-field indexes for it, no composite (the `transcript_records` practice; Δ237)."""
    for op in ops:
        if op[0] == "order_by":
            return False
        if op[0] == "where" and op[2] not in (("==", "in") if op[1] != "__name__" else (">",)):
            return False
    return True


@pytest.fixture
def run():
    env = MagicMock()
    env.drive_mirror_roots_collection = ROOTS
    env.drive_transcript_mirror_collection = LEDGER
    db = NameAwareDb()
    with eval_transactions():
        yield db, FirestoreDriveMirrorRepository(db, env)


async def _everything(repo):
    await repo.ready_marks("u", "d", T, 5)
    await repo.children("u", "d", "p", 5)
    await repo.folder_docs("u", "d", None, 5)
    await repo.folder_docs("u", "d", "u:folder:a", 5)
    await repo.scan_page("u", "d", None, 5)
    await repo.scan_page("u", "d", "u:file:a", 5)
    await repo.detached_due("u", "d", T, 5)
    await repo.by_mirror_item("u", "m")
    await repo.archived_newest("u", 5)
    await repo.archived_by_keys("u", ARCHIVE_FAMILY, ["w:a"], 5)
    await repo.archived_by_keys("u", NAME_FAMILY, ["w:a"], 5)
    await repo.pinned("u", "k")
    await repo.pinned_keys("u")


async def test_every_new_query_has_its_index(run):
    db, repo = run
    await _everything(repo)
    declared = _declared(LIVE_LEDGER)
    sent = db.collection(LEDGER).executed
    # marks 1, children 1, folder walk 2, scan 2, detached 2 (never checked, then old), mirror item 1, newest 1,
    # by keys 2, pinned 2, pinned_keys 2: the pins' two queries each
    assert len(sent) == 1 + 1 + 2 + 2 + 2 + 1 + 1 + 2 + 2 + 2
    *composite, pins_a, pins_b = sent                        # `pinned_keys` is called last: its two queries
    missing = [ops for ops in composite if not _served(ops, declared)]
    assert missing == []
    assert _single_field_served(pins_a) and _single_field_served(pins_b)   # Δ237: no composite for the purge's pins


async def test_the_record_queries_have_their_indexes(mock_env_config):
    db = NameAwareDb()
    with eval_transactions():
        records = make_eval_repo(db, mock_env_config)
        await records.mirror_queue(U_SCOPE, 5)
        await records.mirror_wakeups(U_SCOPE, T, 5)
    declared = _declared(LIVE_RECS)
    queue, wakeups = db.cols[RECS].executed
    assert [("connection_id", "ASCENDING"), ("mirror_due", "ASCENDING"), ("effective_day", "DESCENDING"),
            ("item_id", "ASCENDING")] in declared
    assert [("connection_id", "ASCENDING"), ("mirror_retry_at", "ASCENDING"), ("item_id", "ASCENDING")] in declared
    assert _served(queue, declared) and _served(wakeups, declared)


def test_indexes_declared_with_drive_identity():
    declared = _declared(LIVE_LEDGER)
    base = ("user_id", "ASCENDING"), ("drive_identity", "ASCENDING")
    assert [*base, ("gone_pending", "ASCENDING"), ("mark_ready_at", "ASCENDING")] in declared
    assert [*base, ("mirror_parent_id", "ASCENDING")] in declared
    assert [*base, ("kind", "ASCENDING"), ("__name__", "ASCENDING")] in declared
    assert [*base, ("state", "ASCENDING"), ("checked_at", "ASCENDING")] in declared
    # the pins and the deleted-files search span drives (Δ217, §27.9.11): no drive_identity in them
    spanning = [i for i in declared if ("drive_identity", "ASCENDING") not in i]
    assert [("user_id", "ASCENDING"), ("content_key", "ASCENDING"), ("state", "ASCENDING")] in spanning
    assert [("user_id", "ASCENDING"), ("content_key", "ASCENDING"), ("gone_pending", "ASCENDING")] in spanning
    # `pinned_keys` needs no composite since Δ237; the two it used are gone (each composite costs every ledger write)
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("content_key", "ASCENDING")] not in declared
    assert [("user_id", "ASCENDING"), ("gone_pending", "ASCENDING"), ("content_key", "ASCENDING")] not in declared
    assert [("user_id", "ASCENDING"), ("mirror_item_id", "ASCENDING")] in spanning
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("archived_at", "DESCENDING")] in spanning
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("archive_keys", "CONTAINS")] in spanning
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("name_keys", "CONTAINS")] in spanning
    assert len(declared) == 10


def test_archive_keys_is_exempt_from_single_field_indexing():
    assert "archive_keys" in _exempt(LIVE_LEDGER)


async def test_pins_send_two_queries_each_and_never_an_or(run):
    db, repo = run
    await repo.pinned("u", "k")
    first, second = db.collection(LEDGER).executed
    assert ("where", "state", "in", ["archived", "detached"]) in first
    assert ("where", "gone_pending", "==", True) in second
    db.collection(LEDGER).executed.clear()
    await repo.pinned_keys("u")
    assert len(db.collection(LEDGER).executed) == 2
    for ops in db.collection(LEDGER).executed:
        assert not any(op[0] == "where" and op[1] == "drive_identity" for op in ops)   # per user, every drive


async def test_every_step_query_filters_on_the_drive(run):
    db, repo = run
    await repo.ready_marks("u", "d", T, 5)
    await repo.children("u", "d", "p", 5)
    await repo.folder_docs("u", "d", None, 5)
    await repo.scan_page("u", "d", None, 5)
    await repo.detached_due("u", "d", T, 5)
    for ops in db.collection(LEDGER).executed:
        assert ("where", "drive_identity", "==", "d") in ops and ("where", "user_id", "==", "u") in ops
