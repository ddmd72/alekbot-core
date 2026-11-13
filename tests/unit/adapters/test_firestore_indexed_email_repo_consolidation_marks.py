"""`get_consolidation_marks` of FirestoreIndexedEmailRepository: stored ids with their mark, absent ids left out, the
read is a projection of `consolidated_at`, 500 refs per call. Same fake and setup as the store kit."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)
MARK = datetime(2026, 10, 6, 7, 15)


@pytest.fixture
def repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return FirestoreIndexedEmailRepository(db, env)


def email(email_id, user_id="user-1", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="acct-1", source="gmail",
                  text="Booking confirmed.", vector=[1.0, 0.0, 0.0], tags=["flight"], category="travel",
                  metadata={"subject": "Booking"}, subject="Booking", from_address="noreply@airline.example",
                  email_date=D0, indexed_at=D0)
    fields.update(over)
    return IndexedEmail(**fields)


async def test_present_ids_map_to_their_mark_and_absent_ids_are_left_out(repo):
    await repo.save_batch([email("m1", consolidated_at=MARK), email("m2"), email("m9", user_id="user-2",
                                                                                 consolidated_at=MARK)])

    marks = await repo.get_consolidation_marks("user-1", ["m1", "m2", "m3", "m9"])

    assert marks == {"m1": MARK, "m2": None}


async def test_no_ids_read_nothing(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(repo.db, "get_all", lambda *a, **k: calls.append(1))

    assert await repo.get_consolidation_marks("user-1", []) == {}
    assert calls == []


async def test_the_read_projects_consolidated_at_and_chunks_at_500(repo, monkeypatch):
    seen = []
    real = repo.db.get_all

    def spy(refs, field_paths=None, transaction=None):
        seen.append((len(refs), field_paths))
        return real(refs, field_paths=field_paths, transaction=transaction)

    monkeypatch.setattr(repo.db, "get_all", spy)
    ids = [f"m{i}" for i in range(1201)]
    await repo.save_batch([email("m1200", consolidated_at=MARK)])

    marks = await repo.get_consolidation_marks("user-1", ids)

    assert seen == [(500, ["consolidated_at"]), (500, ["consolidated_at"]), (201, ["consolidated_at"])]
    assert marks == {"m1200": MARK}
