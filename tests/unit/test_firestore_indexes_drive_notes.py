"""Provisioning of the drive-note store's queries (DRIVE_FILE_NOTES_RFC §3.5): every composite a repository method needs
is declared in config/firestore.indexes.json under the live `development_drive_notes` collection, the queries that need
none use only the `user_id` single-field index, and `text`, `keys`, `note_vector` and `snapshot` are exempt from
single-field indexing. The vector index (§3.5 item 6) is S3's script and is not declared here."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.adapters.firestore_drive_note_repo import FirestoreDriveNoteRepository

from tests.unit.adapters.firestore_drive_eval_fakes import eval_transactions
from tests.unit.adapters.firestore_drive_note_fakes import NOTES, NoteDb

_ROOT = Path(__file__).resolve().parents[2]
LIVE = "development_drive_notes"
T = datetime(2026, 10, 21, tzinfo=timezone.utc)


def _raw():
    return json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())


def _declared():
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig")) for f in idx["fields"]]
            for idx in _raw() if idx["collectionGroup"] == LIVE and "fields" in idx]


def _served(ops, declared):
    """A query is served by a composite listing its equality fields (any order) then its orderings, or — equality
    and array filters only — by one listing exactly its filter fields."""
    filters = {op[1] for op in ops if op[0] == "where"}
    orders = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
    tail = [(n, d if d in ("ASCENDING", "DESCENDING") else "ASCENDING") for n, d in orders]
    for index in declared:
        head, rest = index[:len(index) - len(tail)], index[len(index) - len(tail):]
        if tail and rest != tail:
            continue
        if {n for n, _ in head} == filters - {n for n, _ in tail}:
            return True
    return False


@pytest.fixture
def run():
    env = MagicMock()
    env.drive_notes_collection = NOTES
    db = NoteDb()
    with eval_transactions():
        yield db, FirestoreDriveNoteRepository(db, env)


async def test_every_composite_query_has_its_index(run):
    db, repo = run
    await repo.find_bound("u", "i:onedrive:d:x")
    await repo.find_by_match_keys("u", ["r:x"])
    await repo.changed_since("u", None, 5)
    await repo.changed_since("u", T, 5)
    await repo.changed_since("u", T, 5, after_note_id="n1")
    await repo.archived("u", 5)
    await repo.active("u", 5)
    await repo.embedding_pending("u", 5)
    sent = db.collection(NOTES).executed
    assert len(sent) == 8
    declared = _declared()
    assert [ops for ops in sent if not _served(ops, declared)] == []


async def test_the_single_field_queries_filter_only_the_user(run):
    db, repo = run
    await repo.note_ids("u")
    await repo.count("u")
    for ops in db.collection(NOTES).executed:
        assert [op for op in ops if op[0] in ("where", "order_by")] == [("where", "user_id", "==", "u")]


def test_the_declared_composites():
    declared = _declared()
    assert [("user_id", "ASCENDING"), ("match_keys", "CONTAINS")] in declared
    assert [("user_id", "ASCENDING"), ("updated_at", "ASCENDING"), ("note_id", "ASCENDING")] in declared
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("archived_at", "DESCENDING")] in declared
    assert [("user_id", "ASCENDING"), ("state", "ASCENDING"), ("updated_at", "DESCENDING")] in declared
    assert [("user_id", "ASCENDING"), ("embedding_pending", "ASCENDING")] in declared
    assert len(declared) == 5


def test_the_exemptions():
    exempt = {idx["fieldPath"] for idx in _raw()
              if idx["collectionGroup"] == LIVE and "fields" not in idx and idx.get("indexes") == []}
    assert exempt == {"text", "keys", "note_vector", "snapshot"}
    assert "match_keys" not in exempt
