"""The review's and the owner's writes through `FactManagementAdapter.update_fact` (facts S4 · T3a): `context.accepts`
reaches a stale fact, `evidence_at` and `reason` go to `finish_fact_update`, and `history` archives a fact beside a new
history fact in one transaction.

On the evaluating Firestore fake with the index refusal ON, through the real repository, write service and adapter.
Assertions are on the tool result and on the raw stored documents.
"""
import copy
from datetime import date, datetime, timedelta, timezone

import pytest
from google.api_core.exceptions import AlreadyExists

from src.domain.entities import FactState
from src.domain.fact_lifecycle import REVIEW_ACCEPTS, REVIEW_CLOSED_STATES
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_basis import ReviewBasis
from src.domain.fact_review_reasons import answer_reason, basis_reason, history_reason, superseded_reason
from src.domain.fact_tool_result import HISTORY_FACT_ID
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, T0, USER, make_fact, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon

UTC = timezone.utc
ACTORS = ["review", "owner"]
STALE_SINCE = T0 + timedelta(days=40)
REVIVED = basis_reason("revive", ReviewBasis("evidence", evidence_fact_id="e1234567-rest"))
REWRITTEN = basis_reason("rewrite", ReviewBasis("reply", question_id="q7", quote="moved to Lisbon"))
ARCHIVED = basis_reason("archive", ReviewBasis("date_passed", on=date(2026, 2, 1)), date(2026, 2, 1))
INVALIDATED = basis_reason("invalidate", ReviewBasis("reply", question_id="q7", quote="never true"))
ENDED = answer_reason(AnswerKind.ENDED, "q7", "digit")
HISTORY = "No longer true (the owner said so in Oct 2026): works at Acme"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed(store, doc_id, text=None, *, state="stale", **over):
    """A raw fact `doc_id` in `state`: a horizon of 30 days from T0, tags work + consolidated; a stale one has its date."""
    fields = {
        "state": state, "is_current": True, "reported_date": T0, "expiry_kind": "horizon", "ttl_days": 30,
        "expires_at": T0 + timedelta(days=30), "tags": ["work", "consolidated"], "type": "state",
        "temporal_class": "dynamic", "context_priority": "high", "metadata": {"employer": "Acme"},
        "lifecycle_events": [],
    }
    if state == "stale":
        fields["stale_since"] = STALE_SINCE
    fields.update(over)
    return put_raw(store, doc_id, text or doc_id, **fields)


def last_entry(store, doc_id):
    return store.docs[doc_id]["lifecycle_events"][-1]


def recently(value):
    return abs(value - datetime.now(UTC)) < timedelta(minutes=5)


# ------------------------------------------------------------------------------------------------------ revive

@pytest.mark.parametrize("actor", ACTORS)
async def test_a_stale_fact_is_revived_with_the_reason_and_a_later_evidence_date(store, actor):
    seed(store, "old")
    evidence = datetime.now(UTC) - timedelta(days=1)

    res = await store.adapter.update_fact(
        "old", {"state": "current", "expiry": horizon(30)}, ACC, context=ctx(actor), evidence_at=evidence, reason=REVIVED,
    )

    assert res["status"] == "updated" and res["fact_id"] == "old" and HISTORY_FACT_ID not in res, res
    doc = store.docs["old"]
    assert doc["state"] == "current" and doc["stale_since"] is None and doc["reported_date"] == evidence
    assert doc["expires_at"] > datetime.now(UTC) + timedelta(days=29)
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("stale", "current", actor, REVIVED)


@pytest.mark.parametrize("actor", ACTORS)
async def test_a_current_fact_waiting_for_an_answer_accepts_the_review_write(store, actor):
    seed(store, "old", state="current")

    res = await store.adapter.update_fact(
        "old", {"expiry": horizon(60)}, ACC, context=ctx(actor), evidence_at=datetime.now(UTC), reason=REVIVED,
    )

    assert res["status"] == "updated", res
    doc = store.docs["old"]
    assert doc["state"] == "current" and doc["ttl_days"] > 60
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("current", "current", actor, REVIVED)


# ----------------------------------------------------------------------------------------------------- rewrite

@pytest.mark.parametrize("actor", ACTORS)
async def test_a_rewrite_of_a_stale_fact_is_a_current_version_and_supersedes_the_stale_one(store, actor):
    seed(store, "old", "works at Acme")

    res = await store.adapter.update_fact(
        "old", {"content": "works at Beta", "expiry": horizon(90)}, ACC, context=ctx(actor), reason=REWRITTEN,
    )

    assert res["status"] == "updated" and res["fact_id"] != "old" and res["version"] == 2, res
    new = store.docs[res["fact_id"]]
    assert new["state"] == "current" and new["text"] == "works at Beta" and new["stale_since"] is None
    assert new["lifecycle_events"][-1]["reason"] == REWRITTEN and new["lifecycle_events"][-1]["actor"] == actor
    old = store.docs["old"]
    assert old["state"] == "superseded" and old["superseded_by"] == res["fact_id"]


# ----------------------------------------------------------------------------------------------------- archive

@pytest.mark.parametrize("actor", ACTORS)
async def test_an_archive_with_history_writes_both_documents(store, actor):
    seed(store, "old", "works at Acme")
    said = datetime.now(UTC) - timedelta(hours=2)
    before = set(store.docs)

    res = await store.adapter.update_fact(
        "old", {"state": "archived"}, ACC, context=ctx(actor), evidence_at=said, reason=ENDED, history=HISTORY,
    )

    assert res["status"] == "updated" and res["fact_id"] == "old", res
    history_id = res[HISTORY_FACT_ID]
    assert set(store.docs) == before | {history_id}
    old = store.docs["old"]
    assert old["state"] == "archived" and old["is_current"] is True and recently(old["valid_to"])
    assert old["stale_since"] == STALE_SINCE
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("stale", "archived", actor, ENDED)

    new = store.docs[history_id]
    assert new["text"] == HISTORY and new["state"] == "current" and new["is_current"] is True
    assert (new["expiry_kind"], new["ttl_days"], new["expires_at"]) == ("none", None, None)
    assert (new["domain"], new["type"], new["tags"]) == (old["domain"], old["type"], old["tags"])
    assert new["reported_date"] == said
    assert new["account_id"] == ACC and new["created_by_user_id"] == USER
    assert new["lineage_id"] not in (old["lineage_id"], None) and new["version"] == 1
    (born,) = new["lifecycle_events"]
    assert (born["from"], born["to"], born["actor"], born["reason"]) == (None, "current", actor, history_reason("old"))


async def test_a_conflict_on_the_first_attempt_writes_no_history_fact_and_the_retry_writes_one(store):
    seed(store, "old")
    store.interfere("old", {"last_updated": T0 + timedelta(days=50)})   # another writer commits on the first read
    before = set(store.docs)
    attempts = []
    real = store.repo.archive_and_add

    async def counted(*args, **kwargs):
        attempts.append(args[1].id)
        return await real(*args, **kwargs)

    store.repo.archive_and_add = counted

    res = await store.adapter.update_fact(
        "old", {"state": "archived"}, ACC, context=ctx("owner"), evidence_at=datetime.now(UTC), reason=ENDED,
        history=HISTORY,
    )

    assert res["status"] == "updated", res
    assert attempts == [res[HISTORY_FACT_ID]] * 2                    # the first commit lost the race; the same fact again
    assert set(store.docs) == before | {res[HISTORY_FACT_ID]}       # one new document, not one per attempt
    assert store.docs["old"]["state"] == "archived"


@pytest.mark.parametrize("actor", ACTORS)
async def test_an_archive_without_history_writes_the_archive_only(store, actor):
    seed(store, "old")
    before = set(store.docs)

    res = await store.adapter.update_fact("old", {"state": "archived"}, ACC, context=ctx(actor), reason=ARCHIVED)

    assert res["status"] == "updated" and HISTORY_FACT_ID not in res, res
    assert set(store.docs) == before
    doc = store.docs["old"]
    assert doc["state"] == "archived" and doc["stale_since"] == STALE_SINCE
    assert last_entry(store, "old")["reason"] == ARCHIVED


@pytest.mark.parametrize("actor", ACTORS)
async def test_an_invalidate_closes_the_stale_fact(store, actor):
    seed(store, "old")

    res = await store.adapter.update_fact("old", {"state": "invalidated"}, ACC, context=ctx(actor), reason=INVALIDATED)

    assert res["status"] == "updated", res
    assert store.docs["old"]["state"] == "invalidated"
    entry = last_entry(store, "old")
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("stale", "invalidated", actor, INVALIDATED)


# --------------------------------------------------------------------------------------------------- supersede

@pytest.mark.parametrize("actor", ACTORS)
async def test_a_supersede_by_a_current_neighbour_passes(store, actor):
    seed(store, "old")
    seed(store, "near", state="current")

    res = await store.adapter.update_fact(
        "old", {"state": "superseded", "superseded_by": "near"}, ACC, context=ctx(actor), reason=superseded_reason("near"),
    )

    assert res["status"] == "updated", res
    doc = store.docs["old"]
    assert doc["state"] == "superseded" and doc["superseded_by"] == "near" and doc["is_current"] is False
    entry = last_entry(store, "old")
    assert (entry["to"], entry["actor"], entry["reason"]) == ("superseded", actor, superseded_reason("near"))


@pytest.mark.parametrize("actor", ACTORS)
async def test_a_supersede_by_a_stale_neighbour_is_refused_and_nothing_is_written(store, actor):
    seed(store, "old")
    seed(store, "near")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact(
        "old", {"state": "superseded", "superseded_by": "near"}, ACC, context=ctx(actor), reason=superseded_reason("near"),
    )

    assert res["status"] == "failed" and res["message"].startswith("successor fact near is stale"), res
    assert store.docs == before


# ------------------------------------------------------------------------------------------------- the refusals

@pytest.mark.parametrize("actor", ["stage1", "stage2a", "backstop"])
async def test_a_consolidation_context_still_refuses_a_stale_fact(store, actor):
    seed(store, "old")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact("old", {"state": "archived"}, ACC, context=ctx(actor))

    assert res == {"fact_id": "old", "status": "failed", "message": "fact old is stale"}
    assert store.docs == before


@pytest.mark.parametrize(("actor", "updates"), [
    ("owner", {"state": "invalidated"}),
    ("review", {"state": "current", "expiry": horizon(30)}),
    ("owner", {"state": "archived", "expiry": horizon(30)}),
    ("review", {"state": "archived", "tags": ["x"]}),
    ("owner", {"content": "works at Beta", "expiry": horizon(30)}),
    ("stage2a", {"state": "archived"}),
], ids=["invalidate", "revive", "archive-and-expiry", "archive-and-tags", "rewrite", "consolidation"])
async def test_history_with_anything_but_an_exact_review_archive_is_refused_and_nothing_is_written(store, actor, updates):
    seed(store, "old")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact(
        "old", updates, ACC, context=ctx(actor), evidence_at=datetime.now(UTC), reason=ENDED, history=HISTORY,
    )

    assert res["status"] == "failed" and res["message"].startswith("history"), res
    assert "nothing was written" in res["message"]
    assert store.docs == before


async def test_history_without_evidence_at_is_refused_and_nothing_is_written(store):
    seed(store, "old")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact(
        "old", {"state": "archived"}, ACC, context=ctx("owner"), reason=ENDED, history=HISTORY,
    )

    assert res["status"] == "failed" and "evidence_at" in res["message"], res
    assert store.docs == before


@pytest.mark.parametrize("actor", ACTORS)
@pytest.mark.parametrize("updates", [{"state": "archived"}, {"state": "current", "expiry": horizon(30)}])
async def test_a_review_write_without_a_reason_fails_with_the_domains_message(store, actor, updates):
    seed(store, "old")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact("old", updates, ACC, context=ctx(actor))

    assert res["status"] == "failed", res
    assert f"a {actor} write carries its code-built trail reason; none was given" in res["message"]
    assert store.docs == before


async def test_a_history_archive_without_a_reason_fails_and_writes_neither_document(store):
    seed(store, "old")
    before = copy.deepcopy(store.docs)

    res = await store.adapter.update_fact(
        "old", {"state": "archived"}, ACC, context=ctx("owner"), evidence_at=datetime.now(UTC), history=HISTORY,
    )

    assert res["status"] == "failed" and "none was given" in res["message"], res
    assert store.docs == before


# -------------------------------------------------------------------------------- archive_and_add (T3b review)

async def test_archive_and_add_with_a_taken_new_id_raises_already_exists_and_archives_nothing(store):
    seed(store, "old")
    seed(store, "f-new", state="current")
    stored = await store.repo.resolve_current("old", ACC, REVIEW_ACCEPTS)
    before = copy.deepcopy(store.docs)

    with pytest.raises(AlreadyExists):
        await store.repo.archive_and_add(stored, make_fact("new"), datetime.now(UTC), actor="owner", reason=ENDED)

    assert store.docs == before


@pytest.mark.parametrize("state", [FactState.SUPERSEDED, FactState.ARCHIVED, FactState.INVALIDATED])
async def test_archive_and_add_refuses_a_fact_that_is_not_stale_or_current_before_any_write(store, state):
    seed(store, "old")
    stored = await store.repo.resolve_current("old", ACC, REVIEW_ACCEPTS)
    closed = StoredFact(stored.fact.model_copy(update={"state": state}), stored.update_time)
    before = copy.deepcopy(store.docs)

    with pytest.raises(ValueError, match="only a stale or current fact is archived"):
        await store.repo.archive_and_add(closed, make_fact("new"), datetime.now(UTC), actor="owner", reason=ENDED)

    assert store.docs == before


async def test_recently_closed_lists_the_domains_closed_states(store):
    since = datetime.now(UTC) - timedelta(days=1)
    for state in FactState:
        seed(store, f"f-{state.value}", state=state.value, valid_to=datetime.now(UTC))

    closed = await store.repo.get_recently_closed(ACC, since, 10)

    assert {fact.state for fact in closed} == set(REVIEW_CLOSED_STATES) == {FactState.ARCHIVED, FactState.INVALIDATED}
