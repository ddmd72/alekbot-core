"""Email index v2, slice 10: the v1 email vectors, their indexes and their repair are gone, and nothing that still reads a
stored mail breaks on the old fields."""
import json
import re
from datetime import datetime
from pathlib import Path

from src.domain.email import IndexedEmail, IndexingJob
from tests.unit.handlers.test_worker_handler import _make_worker

_ROOT = Path(__file__).resolve().parents[2]
_V1_VECTOR_FIELDS = ("vector", "tags_vector", "metadata_vector", "attachments_vector")
_MAIL_FILE = re.compile(r"email|mail", re.IGNORECASE)
_D = datetime(2026, 10, 5, 10, 0)


def _mail_modules():
    """The src modules of the mail subsystem, by name: the facts' own `tags_vector` and `metadata_vector` are memory."""
    return [p for p in (_ROOT / "src").rglob("*.py") if _MAIL_FILE.search(p.name)]


def test_no_mail_module_names_a_v1_vector_field_or_the_pending_flag():
    modules = _mail_modules()
    assert modules, "the scan must find the mail modules"
    pattern = re.compile(r"\b(tags_vector|metadata_vector|attachments_vector|embedding_pending)\b")
    named = {str(p.relative_to(_ROOT)): sorted(set(pattern.findall(p.read_text()))) for p in modules
             if pattern.search(p.read_text())}
    assert named == {}


def test_no_src_module_names_the_mail_attachments_vector():
    named = [str(p.relative_to(_ROOT)) for p in (_ROOT / "src").rglob("*.py") if "attachments_vector" in p.read_text()]
    assert named == []


def test_the_index_file_declares_no_v1_vector_index_and_keeps_the_card_vector_index():
    entries = json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())
    mail = [e for e in entries if e.get("collectionGroup", "").endswith("domain_email_facts_v1")]
    vector_fields = {f["fieldPath"] for e in mail for f in e.get("fields", []) if "vectorConfig" in f}
    assert vector_fields == {"card_vector"}
    assert not [e for e in mail if any(f["fieldPath"] in _V1_VECTOR_FIELDS for f in e.get("fields", []))]


async def test_the_worker_answers_400_to_the_removed_repair_task_type():
    worker, _ = _make_worker()
    answer, status = await worker.handle({"task_type": "repair_email_embeddings"})
    assert status == 400 and answer == {"error": "unknown task_type"}


def test_an_indexed_email_has_no_vector_fields_and_the_job_no_pending_count():
    assert not set(_V1_VECTOR_FIELDS) & set(IndexedEmail.model_fields)
    assert "embedding_pending" not in IndexedEmail.model_fields
    assert "embedding_pending" not in IndexingJob.model_fields


def test_a_stored_document_that_still_carries_the_old_vectors_parses_and_drops_them():
    stored = dict(email_id="e1", user_id="u1", account_id="a1", source="gmail", text="Invoice FA-0137.", tags=["invoice"],
                  category="finance", metadata={"subject": "Factura FA-0137"}, subject="Factura FA-0137",
                  from_address="shop@example.com", email_date=_D, indexed_at=_D, consolidated_at=None,
                  vector=[0.1, 0.2], tags_vector=[0.3], metadata_vector=[0.4], attachments_vector=None,
                  embedding_pending=True)
    mail = IndexedEmail(**stored)
    dumped = mail.model_dump()
    assert mail.text == "Invoice FA-0137." and mail.tags == ["invoice"] and mail.consolidated_at is None
    assert not {*_V1_VECTOR_FIELDS, "embedding_pending"} & set(dumped)
