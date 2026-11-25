"""The memory inbox store for tests: the real `FirestoreMemoryInboxRepository` over an evaluating database (H1), its two
collections mapped to their declared groups, so a query no declared index serves is refused as Firestore refuses it.

`inbox_store(db=None)`: `db` is the database to build on: the kit passes `kit_db(indexes=INDEXES_FILE, groups=GROUPS)`
(the test database in the cloud gate); service and handler tests pass nothing and get a fresh `EvalDb` with the index
refusal on. One implementation, no hand-written fake (the plan's delta 1, `tests/CLAUDE.md` "Fakes with semantics").
"""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from src.adapters.firestore_memory_inbox_repository import FirestoreMemoryInboxRepository
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[2] / "config" / "firestore.indexes.json"
INBOX = "eval_memory_inbox"
STATE = "eval_memory_inbox_state"
GROUPS: Dict[str, str] = {INBOX: "development_memory_inbox", STATE: "development_memory_inbox_state"}


def inbox_env() -> SimpleNamespace:
    return SimpleNamespace(memory_inbox_collection=INBOX, memory_inbox_state_collection=STATE)


class InboxStore:
    """The database and the repository on it, with the raw stored documents of both collections."""

    def __init__(self, db: Any, repo: FirestoreMemoryInboxRepository) -> None:
        self.db = db
        self.repo = repo

    @property
    def docs(self) -> dict:
        """The inbox's raw documents by id: submissions and candidates, as Firestore holds them."""
        return self.db.collection(INBOX).docs

    @property
    def state_docs(self) -> dict:
        """The raw state documents by user id."""
        return self.db.collection(STATE).docs

    def of_status(self, status: str) -> Dict[str, dict]:
        return {doc_id: doc for doc_id, doc in self.docs.items() if doc.get("status") == status}


def inbox_store(db: Optional[Any] = None) -> InboxStore:
    db = db if db is not None else EvalDb(indexes=INDEXES_FILE, groups=GROUPS)
    return InboxStore(db, FirestoreMemoryInboxRepository(db, inbox_env()))
