"""IndexedEmailRepository kit (P11 · G2): the docstring of `src/ports/indexed_email_repository.py` checked against
`FirestoreIndexedEmailRepository` on the evaluating Firestore fake (H1), with the index refusal ON (vector indexes
included). Assertions are on the raw stored documents and on what readers return.

One strict xfail: P11-G2-1 (a re-saved email loses consolidated_at). The vector search, the pending-embedding reads
and `update_vectors` (and the xfail P11-G2-2 of the vector search) were removed with the v1 vectors (email index v2,
slice 10).
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail, IndexingState
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)                # naive UTC, as the repository returns dates
V1_VECTOR_FIELDS = {"vector", "tags_vector", "metadata_vector", "attachments_vector", "embedding_pending"}


@pytest.fixture
def repo(kit_db):
    db = kit_db(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return FirestoreIndexedEmailRepository(db, env)


def docs(repo):
    return repo.db.collection(FACTS).docs


def email(email_id, user_id="u1", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="a1", source="gmail",
                  text="Invoice for the bathroom repair at 12 Oak Street.", tags=["invoice"],
                  category="finance", metadata={"subject": "Bathroom repair invoice"}, subject="Bathroom repair invoice",
                  from_address="invoices@example.com", email_date=D0, indexed_at=D0)
    fields.update(over)
    return IndexedEmail(**fields)


def ids(emails):
    return [e.email_id for e in emails]


# --------------------------------------------------------------------------------------------------- save

async def test_save_round_trip_stores_no_v1_vector_and_returns_naive_utc_dates(repo):
    sent = email("e1", attachments=["invoice_bathroom_es.pdf"],
                 email_date=datetime(2026, 10, 5, 12, 0, tzinfo=timezone(timedelta(hours=2))))
    assert await repo.save_batch([sent]) == 1
    stored = docs(repo)["u1_e1"]
    assert not V1_VECTOR_FIELDS & set(stored)   # email index v2 slice 10: no v1 vector, no pending flag is written
    got = (await repo.get_unconsolidated_batch("u1"))[0]
    assert got.email_date == datetime(2026, 10, 5, 10, 0) and got.email_date.tzinfo is None
    assert got == sent.model_copy(update={"email_date": datetime(2026, 10, 5, 10, 0)})


@pytest.mark.h1_only("reads the fake's commit log (.commits)")
async def test_save_uses_the_user_scoped_document_id_and_501_emails_are_two_commits(repo):
    assert await repo.save_batch([]) == 0
    assert await repo.save_batch([email(f"e{i}") for i in range(501)]) == 501
    assert len(docs(repo)) == 501 and docs(repo)["u1_e500"]["email_id"] == "e500"
    assert [len(c) for c in repo.db.commits] == [500, 1]


# --------------------------------------------------------------------------------------------------- cursors

async def test_indexing_state_is_none_then_created_then_updated(repo):
    assert await repo.get_indexing_state("u1", "gmail") is None
    await repo.update_indexing_state(IndexingState(user_id="u1", provider="gmail", indexed_through=D0))
    assert await repo.get_indexing_state("u1", "gmail") == IndexingState(user_id="u1", provider="gmail",
                                                                          indexed_through=D0)
    later = D0 + timedelta(days=1)
    await repo.update_indexing_state(IndexingState(user_id="u1", provider="gmail", indexed_through=later,
                                                   cursor_reindex=D0 - timedelta(days=1095)))
    got = await repo.get_indexing_state("u1", "gmail")
    assert (got.indexed_through, got.oldest_indexed_through, got.cursor_reindex) == \
        (later, None, D0 - timedelta(days=1095))
    assert list(repo.db.collection(STATE).docs) == ["u1_gmail"]


async def test_delete_by_user_removes_more_than_500_and_keeps_other_users_and_cursors(repo):
    await repo.save_batch([email(f"e{i}") for i in range(501)] + [email("e1", user_id="u2")])
    await repo.update_indexing_state(IndexingState(user_id="u1", provider="gmail", indexed_through=D0))
    await repo.delete_by_user("u1")
    assert list(docs(repo)) == ["u2_e1"]
    assert await repo.get_indexing_state("u1", "gmail") is not None


# --------------------------------------------------------------------------------------------------- consolidation

async def test_unconsolidated_is_oldest_indexed_first_with_limit_and_served_by_the_index(repo):
    # ids sort against time, so only the ordering by indexed_at gives this order
    await repo.save_batch([email("e1", indexed_at=D0 + timedelta(hours=3)), email("e3", indexed_at=D0),
                           email("e2", indexed_at=D0 + timedelta(hours=2)),
                           email("theirs", user_id="u2", indexed_at=D0 - timedelta(hours=1)),
                           email("done", indexed_at=D0 - timedelta(hours=2), consolidated_at=D0)])
    assert ids(await repo.get_unconsolidated_batch("u1")) == ["e3", "e2", "e1"]
    assert ids(await repo.get_unconsolidated_batch("u1", limit=2)) == ["e3", "e2"]


async def test_a_new_email_is_unconsolidated(repo):
    await repo.save_batch([email("e1", indexed_at=D0 + timedelta(minutes=1)), email("e2")])
    got = await repo.get_unconsolidated_batch("u1")
    assert ids(got) == ["e2", "e1"] and got[0].consolidated_at is None


async def test_mark_consolidated_removes_them_from_the_batch_and_other_users_untouched(repo):
    await repo.save_batch([email("e1"), email("e2"), email("e1", user_id="u2")])
    when = datetime(2026, 10, 6, 8, 0)
    await repo.mark_consolidated("u1", ["e1"], when)
    assert ids(await repo.get_unconsolidated_batch("u1")) == ["e2"]
    assert ids(await repo.get_unconsolidated_batch("u2")) == ["e1"]
    assert docs(repo)["u1_e1"]["consolidated_at"] == when.replace(tzinfo=timezone.utc)


async def test_mark_consolidated_on_a_deleted_fact_raises_not_found_and_applies_nothing(repo):
    await repo.save_batch([email("e1")])
    raised = None
    try:
        await repo.mark_consolidated("u1", ["e1", "gone"], D0)
    except NotFound as exc:
        raised = exc
    assert raised is not None and docs(repo)["u1_e1"]["consolidated_at"] is None


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G2-1: re-saving a consolidated email clears consolidated_at, so it is consolidated again"))
async def test_re_saving_a_consolidated_email_keeps_it_consolidated(repo):
    """Flips when save_batch carries a stored non-null consolidated_at into the re-saved document."""
    await repo.save_batch([email("e1")])
    await repo.mark_consolidated("u1", ["e1"], D0)
    require(await repo.get_unconsolidated_batch("u1") == [], "e1 is consolidated before the re-save")
    await repo.save_batch([email("e1")])
    assert await repo.get_unconsolidated_batch("u1") == []
