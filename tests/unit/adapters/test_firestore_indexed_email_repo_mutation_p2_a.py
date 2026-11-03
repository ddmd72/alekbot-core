"""Mutation survivors of `firestore_indexed_email_repo` (P2-a): a missing state's reconnect flags are logged, not
raised; `clear_indexing_state` deletes the document; `count_by_user` is scoped by user and provider; one unparsable
document does not drop the rest of an unconsolidated batch. Same fake and setup as the store kit."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters import firestore_indexed_email_repo as module
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail, IndexingState
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)


@pytest.fixture
def repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return FirestoreIndexedEmailRepository(db, env)


def email(email_id, user_id="user-1", source="gmail", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="acct-1", source=source,
                  text="Invoice for the repair.", vector=[1.0, 0.0, 0.0], tags=["invoice"], category="finance",
                  metadata={"subject": "Invoice"}, subject="Invoice", from_address="invoices@example.com",
                  email_date=D0, indexed_at=D0)
    fields.update(over)
    return IndexedEmail(**fields)


class _Recorder:
    def __init__(self):
        self.debug_lines = []

    def debug(self, msg, *a, **k):
        self.debug_lines.append(str(msg))

    def info(self, *a, **k): pass
    def error(self, *a, **k): pass
    def warning(self, *a, **k): pass


async def test_flags_on_a_missing_state_are_reported_in_the_debug_log(repo, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(module, "logger", rec)
    await repo.set_reconnect_flags("user-1", "gmail", reconnect_notified=True)
    assert repo.db.collection(STATE).docs == {}
    assert len(rec.debug_lines) == 1
    assert "No indexing state" in rec.debug_lines[0] and "reconnect_notified" in rec.debug_lines[0]


async def test_clear_indexing_state_deletes_only_that_users_provider_document(repo):
    for user, provider in (("user-1", "gmail"), ("user-1", "outlook"), ("user-2", "gmail")):
        await repo.update_indexing_state(IndexingState(user_id=user, provider=provider, indexed_through=D0))
    await repo.clear_indexing_state("user-1", "gmail")
    assert await repo.get_indexing_state("user-1", "gmail") is None
    assert set(repo.db.collection(STATE).docs) == {"user-1_outlook", "user-2_gmail"}


async def test_count_by_user_is_scoped_to_the_user_and_optionally_the_provider(repo):
    await repo.save_batch([email("e1"), email("e2"), email("e3", source="outlook"),
                           email("e4", user_id="user-2"), email("e5", user_id="user-2", source="outlook")])
    assert await repo.count_by_user("user-1") == 3
    assert await repo.count_by_user("user-2") == 2
    assert await repo.count_by_user("user-1", "gmail") == 2
    assert await repo.count_by_user("user-1", "outlook") == 1
    assert await repo.count_by_user("user-3") == 0


async def test_an_unparsable_document_does_not_drop_the_rest_of_the_batch(repo):
    await repo.save_batch([email("good-1", indexed_at=D0), email("good-2", indexed_at=D0.replace(hour=12))])
    # a document written by something else: right user, never consolidated, but not a valid IndexedEmail
    await repo.collection.document("user-1_bad").set(
        {"user_id": "user-1", "consolidated_at": None, "indexed_at": D0.replace(hour=11)})
    got = await repo.get_unconsolidated_batch("user-1")
    assert [e.email_id for e in got] == ["good-1", "good-2"]
