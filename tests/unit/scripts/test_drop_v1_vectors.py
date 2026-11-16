"""The v1 vector cleanup script (email index v2, slice 10): a dry run writes nothing; `--live` deletes the five v1 fields
and nothing else; the report is counts only."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from google.cloud.firestore import DELETE_FIELD

from tests.support.firestore_eval import EvalDb

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "email" / "drop_v1_vectors.py"
FACTS = "eval_email_facts"
_D = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
V1 = ("vector", "tags_vector", "metadata_vector", "attachments_vector", "embedding_pending")


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("drop_v1_vectors", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(email_id, **extra):
    return {"email_id": email_id, "user_id": "u1", "text": "Invoice FA-0137 from the hardware shop.", "tags": ["invoice"],
            "category": "finance", "metadata": {"subject": "Factura FA-0137"}, "subject": "Factura FA-0137",
            "state": "current", "indexed_at": _D, "consolidated_at": _D, "card_state": "carded",
            "card": {"title": "Factura FA-0137"}, "card_vector": [0.5, 0.5], "card_vector_pending": False, **extra}


def _old(email_id):
    return _doc(email_id, vector=[0.1], tags_vector=[0.2], metadata_vector=[0.3], attachments_vector=[0.4],
                embedding_pending=False)


async def _seed(db):
    col = db.collection(FACTS)
    await col.document("u1_old1").set(_old("old1"))
    await col.document("u1_old2").set(_doc("old2", vector=[0.1], embedding_pending=True))
    await col.document("u1_new1").set(_doc("new1"))
    return col


def _stored(db, doc_id):
    return dict(db.collection(FACTS).docs[doc_id])


async def test_a_dry_run_counts_and_writes_nothing(script):
    db = EvalDb()
    col = await _seed(db)
    before = {k: _stored(db, k) for k in ("u1_old1", "u1_old2", "u1_new1")}
    commits = len(db.commits)

    counts = await script.drop_v1_vectors(db, col, DELETE_FIELD, live=False)

    assert counts == {"scanned": 3, "with_v1_fields": 2, "updated": 0}
    assert {k: _stored(db, k) for k in before} == before
    assert len(db.commits) == commits


async def test_live_deletes_the_five_fields_and_nothing_else(script):
    db = EvalDb()
    col = await _seed(db)
    before = {k: _stored(db, k) for k in ("u1_old1", "u1_old2", "u1_new1")}

    counts = await script.drop_v1_vectors(db, col, DELETE_FIELD, live=True)

    assert counts == {"scanned": 3, "with_v1_fields": 2, "updated": 2}
    for doc_id, was in before.items():
        now = _stored(db, doc_id)
        assert not set(V1) & set(now)
        assert now == {k: v for k, v in was.items() if k not in V1}
    kept = _stored(db, "u1_old1")
    assert kept["text"] and kept["tags"] and kept["metadata"] and kept["consolidated_at"] == _D
    assert kept["card"] == {"title": "Factura FA-0137"} and kept["card_vector"] == [0.5, 0.5]
    assert kept["card_state"] == "carded" and kept["state"] == "current"


async def test_a_second_live_run_finds_nothing_to_do(script):
    db = EvalDb()
    col = await _seed(db)
    await script.drop_v1_vectors(db, col, DELETE_FIELD, live=True)
    assert await script.drop_v1_vectors(db, col, DELETE_FIELD, live=True) == {
        "scanned": 3, "with_v1_fields": 0, "updated": 0}


async def test_more_than_500_documents_are_updated_in_batches(script):
    db = EvalDb()
    col = db.collection(FACTS)
    for i in range(501):
        await col.document(f"u1_m{i}").set(_doc(f"m{i}", vector=[0.1]))
    counts = await script.drop_v1_vectors(db, col, DELETE_FIELD, live=True)
    assert counts == {"scanned": 501, "with_v1_fields": 501, "updated": 501}
    assert not any("vector" in d for d in col.docs.values())


def test_the_report_is_counts_only(script):
    counts = {"scanned": 3, "with_v1_fields": 2, "updated": 2}
    live, dry = script.format_report(counts, live=True), script.format_report(counts, live=False)
    assert live.splitlines() == ["documents scanned:        3", "documents with v1 fields: 2",
                                 "documents updated:        2"]
    assert "nothing written" in dry and "updated:" not in dry
