"""
Unit tests for scripts/migration/migrate_to_embedding_v2.py.

Covers:
  - Pure text composition helpers (format_metadata_text, format_tags_text,
    task_content_text_from_dict, task_context_text_from_dict).
  - collection_prefix env resolution.
  - migrate_facts orchestration (mock Firestore + mock embedding adapter).
  - migrate_tasks orchestration (mock Firestore + mock embedding adapter).
  - flag_emails_for_repair orchestration (mock Firestore batch).
  - Dry-run vs live semantics: no writes when --live is absent.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Load the script as a module without executing CLI
# ──────────────────────────────────────────────────────────────────────────────
_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scripts" / "migration" / "migrate_to_embedding_v2.py"
)
_spec = importlib.util.spec_from_file_location("migrate_to_embedding_v2", _SCRIPT_PATH)
mig = importlib.util.module_from_spec(_spec)
sys.modules["migrate_to_embedding_v2"] = mig
_spec.loader.exec_module(mig)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — async iterator stand-in for Firestore `query.stream()`
# ──────────────────────────────────────────────────────────────────────────────

class _FakeAsyncStream:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        self._i = 0
        return self

    async def __anext__(self):
        if self._i >= len(self._docs):
            raise StopAsyncIteration
        doc = self._docs[self._i]
        self._i += 1
        return doc


def _make_doc(doc_id, data):
    doc = MagicMock()
    doc.id = doc_id
    doc.to_dict = MagicMock(return_value=data)
    doc.reference = MagicMock()
    doc.reference.update = AsyncMock()
    return doc


def _make_query(docs):
    q = MagicMock()
    q.stream = MagicMock(return_value=_FakeAsyncStream(docs))
    q.limit = MagicMock(return_value=q)
    return q


def _make_db(collection_name_to_docs):
    db = MagicMock()

    def collection(name):
        return _make_query(collection_name_to_docs.get(name, []))

    db.collection = MagicMock(side_effect=collection)

    batch = MagicMock()
    batch.update = MagicMock()
    batch.commit = AsyncMock()
    db.batch = MagicMock(return_value=batch)
    db._test_batch = batch  # expose so tests can assert
    return db


# ──────────────────────────────────────────────────────────────────────────────
# format_metadata_text
# ──────────────────────────────────────────────────────────────────────────────

def test_format_metadata_text_empty_returns_empty():
    assert mig.format_metadata_text({}) == ""
    assert mig.format_metadata_text(None) == ""


def test_format_metadata_text_skips_empty_values():
    out = mig.format_metadata_text({"a": "x", "b": "", "c": None, "d": "y"})
    assert out == "a: x. d: y"


def test_format_metadata_text_joins_with_period_space():
    out = mig.format_metadata_text({"key1": "v1", "key2": "v2"})
    assert ". " in out
    assert "key1: v1" in out
    assert "key2: v2" in out


# ──────────────────────────────────────────────────────────────────────────────
# format_tags_text
# ──────────────────────────────────────────────────────────────────────────────

def test_format_tags_text_empty_returns_empty():
    assert mig.format_tags_text([]) == ""
    assert mig.format_tags_text(None) == ""


def test_format_tags_text_joins_with_comma_space():
    assert mig.format_tags_text(["one", "two", "three"]) == "one, two, three"


# ──────────────────────────────────────────────────────────────────────────────
# collection_prefix
# ──────────────────────────────────────────────────────────────────────────────

def test_collection_prefix_dev():
    assert mig.collection_prefix("dev") == "development_"


def test_collection_prefix_prod():
    assert mig.collection_prefix("prod") == ""


def test_collection_prefix_unknown_raises():
    with pytest.raises(ValueError, match="Unknown env"):
        mig.collection_prefix("staging")


# ──────────────────────────────────────────────────────────────────────────────
# task text composers
# ──────────────────────────────────────────────────────────────────────────────

def test_task_content_text_from_dict_uses_title():
    assert mig.task_content_text_from_dict({"title": "Buy milk"}) == "Buy milk"


def test_task_content_text_from_dict_missing_title_returns_empty():
    assert mig.task_content_text_from_dict({}) == ""


def test_task_context_text_from_dict_combines_list_tags_importance():
    out = mig.task_context_text_from_dict({
        "list_name": "Personal",
        "tags": ["home", "urgent"],
        "importance": "high",
    })
    parts = out.split(" ")
    assert "Personal" in parts
    assert "home" in parts
    assert "urgent" in parts
    assert "high" in parts


def test_task_context_text_from_dict_missing_fields_omitted():
    assert mig.task_context_text_from_dict({"list_name": "X"}) == "X"
    assert mig.task_context_text_from_dict({}) == ""


# ──────────────────────────────────────────────────────────────────────────────
# migrate_facts orchestration
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migrate_facts_dry_run_does_not_write():
    docs = [
        _make_doc("f1", {"text": "fact one", "tags": ["a"], "metadata": {"k": "v"}}),
        _make_doc("f2", {"text": "fact two", "tags": [], "metadata": {}}),
    ]
    db = _make_db({"development_domain_facts_v2": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(return_value=[[0.1]*4, [0.2]*4, [0.3]*4])

    stats = await mig.migrate_facts(db, embedding, "dev", limit=None, live=False)

    assert stats.total == 2
    assert stats.updated == 0
    assert stats.skipped == 2
    # No writes despite embedding being called
    docs[0].reference.update.assert_not_called()
    docs[1].reference.update.assert_not_called()


@pytest.mark.asyncio
async def test_migrate_facts_live_writes_three_vectors_per_doc():
    docs = [_make_doc("f1", {"text": "fact one", "tags": ["a", "b"],
                             "metadata": {"k": "v"}})]
    db = _make_db({"development_domain_facts_v2": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(
        return_value=[[0.1]*4, [0.2]*4, [0.3]*4]
    )

    stats = await mig.migrate_facts(db, embedding, "dev", limit=None, live=True)

    assert stats.updated == 1
    assert stats.errors == 0
    embedding.get_embeddings_batch.assert_called_once()
    # Verify the three texts passed: fact text, tags joined, metadata composed
    args, kwargs = embedding.get_embeddings_batch.call_args
    texts = args[0] if args else kwargs["texts"]
    assert texts[0] == "fact one"
    assert texts[1] == "a, b"
    assert texts[2] == "k: v"

    update_call = docs[0].reference.update.call_args
    update_data = update_call.args[0] if update_call.args else update_call.kwargs
    assert "vector" in update_data
    assert "tags_vector" in update_data
    assert "metadata_vector" in update_data


@pytest.mark.asyncio
async def test_migrate_facts_uses_prod_prefix():
    db = _make_db({"domain_facts_v2": []})
    embedding = MagicMock()

    await mig.migrate_facts(db, embedding, "prod", limit=None, live=True)

    db.collection.assert_called_with("domain_facts_v2")


@pytest.mark.asyncio
async def test_migrate_facts_embed_failure_increments_errors():
    docs = [_make_doc("f1", {"text": "x", "tags": [], "metadata": {}})]
    db = _make_db({"development_domain_facts_v2": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(side_effect=RuntimeError("api down"))

    stats = await mig.migrate_facts(db, embedding, "dev", limit=None, live=True)

    assert stats.errors == 1
    assert "f1" in stats.errors_by_id
    docs[0].reference.update.assert_not_called()


@pytest.mark.asyncio
async def test_migrate_facts_handles_missing_fields():
    """A doc without text/tags/metadata fields must not blow up — falls back to defaults."""
    docs = [_make_doc("f1", {})]
    db = _make_db({"development_domain_facts_v2": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(return_value=[[0.1]*4, [0.2]*4, [0.3]*4])

    stats = await mig.migrate_facts(db, embedding, "dev", limit=None, live=True)

    assert stats.errors == 0
    args, kwargs = embedding.get_embeddings_batch.call_args
    texts = args[0] if args else kwargs["texts"]
    assert texts[0] == ""              # empty text → still passed
    assert texts[1] == "no tags"       # tags fallback
    assert texts[2] == "no metadata"   # metadata fallback


# ──────────────────────────────────────────────────────────────────────────────
# migrate_tasks orchestration
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migrate_tasks_live_writes_two_vectors_per_doc():
    docs = [_make_doc("t1", {
        "title": "Buy milk",
        "list_name": "Personal",
        "tags": ["home"],
        "importance": "normal",
    })]
    db = _make_db({"development_task_search_index": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(return_value=[[0.5]*4, [0.6]*4])

    stats = await mig.migrate_tasks(db, embedding, "dev", limit=None, live=True)

    assert stats.updated == 1
    update_call = docs[0].reference.update.call_args
    update_data = update_call.args[0] if update_call.args else update_call.kwargs
    assert "content_vector" in update_data
    assert "context_vector" in update_data
    assert "vector" not in update_data  # tasks have only 2 vectors


@pytest.mark.asyncio
async def test_migrate_tasks_dry_run_does_not_write():
    docs = [_make_doc("t1", {"title": "X"})]
    db = _make_db({"development_task_search_index": docs})
    embedding = MagicMock()
    embedding.get_embeddings_batch = AsyncMock(return_value=[[0.5]*4, [0.6]*4])

    stats = await mig.migrate_tasks(db, embedding, "dev", limit=None, live=False)

    assert stats.skipped == 1
    docs[0].reference.update.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# flag_emails_for_repair orchestration
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flag_emails_for_repair_live_sets_embedding_pending_true():
    docs = [_make_doc(f"e{i}", {"subject": f"hi {i}"}) for i in range(3)]
    db = _make_db({"development_domain_email_facts_v1": docs})

    stats = await mig.flag_emails_for_repair(db, "dev", limit=None, live=True)

    assert stats.total == 3
    assert stats.updated == 3
    # Three batch.update calls, each with embedding_pending=True
    batch = db._test_batch
    assert batch.update.call_count == 3
    for call in batch.update.call_args_list:
        ref, payload = call.args
        assert payload == {"embedding_pending": True}
    batch.commit.assert_awaited()


@pytest.mark.asyncio
async def test_flag_emails_for_repair_dry_run_no_commit():
    docs = [_make_doc(f"e{i}", {}) for i in range(2)]
    db = _make_db({"development_domain_email_facts_v1": docs})

    stats = await mig.flag_emails_for_repair(db, "dev", limit=None, live=False)

    assert stats.skipped == 2
    assert stats.updated == 0
    batch = db._test_batch
    batch.update.assert_not_called()
    batch.commit.assert_not_awaited()
