"""The gate's v1 fields are gone from the stored mail (email index v2 slice 9b, D3): `IndexedEmail` has no `text`, `tags`,
`category` or `metadata`, `save_batch` writes none of them, and an old document that still carries them reads back
through `memory_batch` (the repository selects only the model's fields; pydantic ignores any other). Over the
evaluating fake with the declared indexes."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from src.domain.email_memory_candidate import SETTLED_FOR_MEMORY
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
V1_FIELDS = ("text", "tags", "category", "metadata")
SENT = datetime(2026, 9, 14, 7, 45)
INDEXED = datetime(2026, 9, 14, 8, 0)
STALE_BEFORE = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)


def _repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    return db, FirestoreIndexedEmailRepository(db, SimpleNamespace(domain_email_facts_collection=FACTS,
                                                                   email_indexing_state_collection=STATE))


def _mail(email_id: str) -> IndexedEmail:
    return IndexedEmail(email_id=email_id, user_id="user-1", account_id="acct-1", source="gmail",
                        subject="Parents' meeting", from_address="Example School <office@school.example>",
                        email_date=SENT, indexed_at=INDEXED)


def test_the_model_has_none_of_the_v1_fields():
    assert not set(V1_FIELDS) & set(IndexedEmail.model_fields)


async def test_a_new_record_is_stored_without_them():
    db, repo = _repo()

    await repo.save_batch([_mail("msg_school02")])

    (doc,) = db.collection(FACTS).docs.values()
    assert not set(V1_FIELDS) & set(doc) and doc["subject"] == "Parents' meeting"


async def test_an_old_document_with_them_reads_through_the_memory_batch():
    db, repo = _repo()
    await repo.save_batch([_mail("msg_school02")])
    db.collection(FACTS).docs["user-1_msg_school02"].update(
        text="User was invited to the parents' meeting.", tags=["school"], category="personal",
        metadata={"subject": "Parents' meeting", "snippet": "The parents' meeting is on 3 March 2026.", "labels": ["INBOX"]})

    (got,) = await repo.memory_batch("user-1", 10, SETTLED_FOR_MEMORY, STALE_BEFORE)

    assert got.email.email_id == "msg_school02" and got.email.subject == "Parents' meeting"
    assert got.card_state == "" and got.card_doc is None
    assert not any(hasattr(got.email, f) for f in V1_FIELDS)
