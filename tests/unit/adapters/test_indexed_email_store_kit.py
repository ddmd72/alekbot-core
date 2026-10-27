"""IndexedEmailRepository kit (P11 · G2): the docstring of `src/ports/indexed_email_repository.py` checked against
`FirestoreIndexedEmailRepository` on the evaluating Firestore fake (H1), with the index refusal ON (vector indexes
included). Assertions are on the raw stored documents and on what readers return.

Two strict xfails: P11-G2-1 (a re-saved email loses consolidated_at) and P11-G2-2 (every vector query failing reads as
"no emails"). The naive date filter of the production caller (`email_search_agent.py`) relies on the H1 rule of card H
(parity scenario `naive_datetime_filter_is_utc`).
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound, ServiceUnavailable

from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail, IndexingState
from tests.support.firestore_eval import EvalDb, EvalQuery, stored_vector
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)                # naive UTC, as the repository returns dates
X, Y, Z = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]
NEAR_X = [1.0, 0.1, 0.0]                          # cosine distance to X ~ 0.005


@pytest.fixture
def repo():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return FirestoreIndexedEmailRepository(db, env)


def docs(repo):
    return repo.db.collection(FACTS).docs


def email(email_id, user_id="u1", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="a1", source="gmail",
                  text="Clara sent invoice FA-0137 for the bathroom repair.", vector=X, tags=["invoice"],
                  category="finance", metadata={"subject": "Invoice FA-0137"}, subject="Invoice FA-0137",
                  from_address="clara@example.com", email_date=D0, indexed_at=D0)
    fields.update(over)
    return IndexedEmail(**fields)


def ids(emails):
    return [e.email_id for e in emails]


# --------------------------------------------------------------------------------------------------- save

async def test_save_round_trip_unwraps_vectors_and_returns_naive_utc_dates(repo):
    sent = email("e1", tags_vector=Y, attachments=["invoice-FA-0137.pdf"],
                 email_date=datetime(2026, 10, 5, 12, 0, tzinfo=timezone(timedelta(hours=2))))
    assert await repo.save_batch([sent]) == 1
    stored = docs(repo)["u1_e1"]
    assert stored_vector(stored["vector"]) == X and stored["metadata_vector"] is None
    got = (await repo.find_nearest("u1", {"vector": X}))[0]
    assert got.vector == X and got.tags_vector == Y and isinstance(got.vector, list)
    assert got.email_date == datetime(2026, 10, 5, 10, 0) and got.email_date.tzinfo is None
    assert got == sent.model_copy(update={"email_date": datetime(2026, 10, 5, 10, 0)})


async def test_save_uses_the_user_scoped_document_id_and_501_emails_are_two_commits(repo):
    assert await repo.save_batch([]) == 0
    assert await repo.save_batch([email(f"e{i}") for i in range(501)]) == 501
    assert len(docs(repo)) == 501 and docs(repo)["u1_e500"]["email_id"] == "e500"
    assert [len(c) for c in repo.db.commits] == [500, 1]


# --------------------------------------------------------------------------------------------------- find_nearest

async def test_find_nearest_scopes_to_the_user_and_current_state(repo):
    await repo.save_batch([email("e1"), email("e2", user_id="u2"), email("e3", state="archived")])
    assert ids(await repo.find_nearest("u1", {"vector": X})) == ["e1"]
    assert ids(await repo.find_nearest("u1", {"vector": X}, state="archived")) == ["e3"]


async def test_find_nearest_with_an_aware_date_range_is_served_by_the_declared_index(repo):
    await repo.save_batch([email("early", email_date=D0 - timedelta(days=3)), email("in", email_date=D0),
                           email("late", email_date=D0 + timedelta(days=3))])
    got = await repo.find_nearest("u1", {"vector": X, "tags_vector": None},
                                  date_from=datetime(2026, 10, 4, tzinfo=timezone.utc),
                                  date_to=datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert ids(got) == ["in"]
    assert ids(await repo.find_nearest("u1", {"vector": X}, date_from=datetime(2026, 10, 4, tzinfo=timezone.utc))) \
        in (["in", "late"], ["late", "in"])


async def test_find_nearest_with_naive_dates_matches_as_utc(repo):
    await repo.save_batch([email("e1", email_date=datetime(2026, 10, 5, 10, 0))])
    assert ids(await repo.find_nearest("u1", {"vector": X}, date_from=datetime(2026, 10, 5, 9, 0))) == ["e1"]
    assert ids(await repo.find_nearest("u1", {"vector": X}, date_from=datetime(2026, 10, 5, 10, 0),
                                       date_to=datetime(2026, 10, 5, 10, 0))) == ["e1"]
    assert await repo.find_nearest("u1", {"vector": X}, date_to=datetime(2026, 10, 5, 9, 30)) == []


async def test_find_nearest_fuses_by_rrf_a_fact_found_by_two_channels_ranks_first(repo):
    await repo.save_batch([email("one", vector=NEAR_X, tags_vector=Z),
                           email("both", vector=[1.0, 0.2, 0.0], tags_vector=Y)])
    # "vector" ranks one before both; only "tags_vector" also finds both: both wins on the fused rank
    assert ids(await repo.find_nearest("u1", {"vector": X, "tags_vector": Y})) == ["both", "one"]


async def test_find_nearest_returns_at_most_limit(repo):
    await repo.save_batch([email(f"e{i}", vector=[1.0, 0.01 * i, 0.0]) for i in range(5)])
    assert ids(await repo.find_nearest("u1", {"vector": X}, limit=2)) == ["e0", "e1"]


async def test_find_nearest_drops_a_fact_beyond_the_similarity_floor(repo):
    await repo.save_batch([email("near", vector=NEAR_X), email("at_45_degrees", vector=[1.0, 1.0, 0.0]),
                           email("orthogonal", vector=Y)])
    assert ids(await repo.find_nearest("u1", {"vector": X})) == ["near", "at_45_degrees"]   # distance 0.29 kept
    await repo.save_batch([email("at_60_degrees", vector=[1.0, 1.7320508, 0.0])])           # distance 0.5 dropped
    assert "at_60_degrees" not in ids(await repo.find_nearest("u1", {"vector": X}))


async def test_find_nearest_skips_none_vectors_and_empty_vectors_query_nothing(repo):
    await repo.save_batch([email("e1")])
    assert await repo.find_nearest("u1", {}) == []
    assert await repo.find_nearest("u1", {"vector": None, "tags_vector": None}) == []
    assert repo.db.collection(FACTS).executed == []
    assert ids(await repo.find_nearest("u1", {"vector": X, "tags_vector": None})) == ["e1"]
    assert [next(op for op in q if op[0] == "find_nearest")[1]["vector_field"]
            for q in repo.db.collection(FACTS).executed] == ["vector"]


async def test_find_nearest_leaves_out_an_unparseable_fact(repo):
    await repo.save_batch([email("e1"), email("e2", vector=NEAR_X)])
    del docs(repo)["u1_e2"]["subject"]
    assert ids(await repo.find_nearest("u1", {"vector": X})) == ["e1"]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G2-2: every vector query failing reads as no match ('No emails found')")
async def test_find_nearest_raises_when_every_channel_fails(repo, monkeypatch):
    """Flips when find_nearest re-raises after gather if every channel raised (one line after the gather)."""
    await repo.save_batch([email("e1", tags_vector=Y)])
    require(ids(await repo.find_nearest("u1", {"vector": X, "tags_vector": Y})) == ["e1"], "the healthy search finds e1")
    refused = record_refusals(monkeypatch)
    repo.db.collection(FACTS).fail = ServiceUnavailable("503 UNAVAILABLE firestore down")
    raised = None
    try:
        await repo.find_nearest("u1", {"vector": X, "tags_vector": Y})
    except Exception as exc:     # noqa: BLE001 - any raise is the outcome; the type is the fix's choice
        raised = exc
    require(refused == ["vector", "tags_vector"] or refused == ["tags_vector", "vector"],
            "both channel queries were refused by the failing collection")
    assert raised is not None


def record_refusals(monkeypatch):
    """The vector field of every facts query that raised ServiceUnavailable, in order."""
    refused = []
    original = EvalQuery.get

    async def get(query, transaction=None):
        try:
            return await original(query, transaction=transaction)
        except ServiceUnavailable:
            if query.group is None and query.cols[0].name == FACTS:
                refused.extend(op[1]["vector_field"] for op in query.ops if op[0] == "find_nearest")
            raise

    monkeypatch.setattr(EvalQuery, "get", get)
    return refused


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


# --------------------------------------------------------------------------------------------------- embedding repair

async def test_pending_embeddings_are_cross_user_with_limit_and_returned_until_repaired(repo):
    await repo.save_batch([email("a", vector=None, embedding_pending=True),
                           email("b", user_id="u2", vector=None, embedding_pending=True),
                           email("c", vector=None, embedding_pending=True), email("done")])
    all_pending = await repo.get_pending_embeddings()
    assert sorted((e.user_id, e.email_id) for e in all_pending) == [("u1", "a"), ("u1", "c"), ("u2", "b")]
    assert len(await repo.get_pending_embeddings(limit=2)) == 2
    assert len(await repo.get_pending_embeddings()) == 3          # nothing repaired: returned again


async def test_pending_embeddings_leave_out_an_unparseable_fact(repo):
    await repo.save_batch([email("a", vector=None, embedding_pending=True),
                           email("b", vector=None, embedding_pending=True)])
    del docs(repo)["u1_b"]["text"]
    assert ids(await repo.get_pending_embeddings()) == ["a"]


async def test_update_vectors_writes_given_vectors_and_clears_pending_even_when_partial(repo):
    await repo.save_batch([email("a", vector=None, embedding_pending=True)])
    await repo.update_vectors("u1", "a", {"vector": X, "tags_vector": None})
    stored = docs(repo)["u1_a"]
    assert stored_vector(stored["vector"]) == X and stored["tags_vector"] is None
    assert stored["embedding_pending"] is False
    assert await repo.get_pending_embeddings() == []


async def test_update_vectors_on_a_missing_fact_raises_not_found(repo):
    raised = None
    try:
        await repo.update_vectors("u1", "gone", {"vector": X})
    except NotFound as exc:
        raised = exc
    assert raised is not None and docs(repo) == {}
