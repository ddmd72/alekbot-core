"""Stored mail for the tests of Stage 3 of consolidation (the email pass): the real `FirestoreIndexedEmailRepository`,
which is both the v1 repository the pass marks mail consolidated through and the `EmailMemoryBatches` it reads mail
through (email index v2 §6.7), over the evaluating Firestore fake with the declared indexes.

A mail saved here has no card fields; once its `indexed_at` is older than `MEMORY_CARD_WAIT` the pass sends it in the
header form (`header_candidate`, slice 9b). Every example is invented (`tests/CLAUDE.md` § Invented cast).
"""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional, Sequence

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
MAIL, STATE = "eval_email_facts", "eval_email_state"


def mail_store() -> FirestoreIndexedEmailRepository:
    """An empty mail store on its own evaluating database, with the index refusal on."""
    db = EvalDb(indexes=INDEXES_FILE, groups={MAIL: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    return FirestoreIndexedEmailRepository(db, SimpleNamespace(domain_email_facts_collection=MAIL,
                                                               email_indexing_state_collection=STATE))


def stored_mail(email_id: str, *, user_id: str, account_id: str, sent: datetime,
                indexed: Optional[datetime] = None, subject: Optional[str] = None,
                from_address: str = "a@example.com") -> IndexedEmail:
    """A mail record with no card; indexed when it was sent unless `indexed` says otherwise."""
    return IndexedEmail(email_id=email_id, user_id=user_id, account_id=account_id, source="gmail",
                        subject=email_id if subject is None else subject, from_address=from_address,
                        email_date=sent, indexed_at=sent if indexed is None else indexed)


async def mail_store_with(*mails: IndexedEmail) -> FirestoreIndexedEmailRepository:
    store = mail_store()
    if mails:
        await store.save_batch(list(mails))
    return store


async def marked(store: FirestoreIndexedEmailRepository, user_id: str, email_ids: Sequence[str]) -> List[str]:
    """The ids of `email_ids` the store holds marked consolidated, in the order given."""
    found = await store.get_consolidation_marks(user_id, list(email_ids))
    return [email_id for email_id in email_ids if found.get(email_id) is not None]
