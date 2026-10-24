"""The `reported_date` rules of Facts S3 (plan §3.5) through `FactManagementAdapter`: a conversation pass clamps the
model's date into the batch span, a cleanup pass keeps the system's date and says so, `derived_from` lends a created fact
its sources' date. Real adapter, write service and repository on the evaluating Firestore fake; assertions are on the raw
stored documents and the returned dicts.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.support.fact_kit import ACC, OTHER, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

NOW = datetime.now(timezone.utc)
FIRST = NOW - timedelta(days=20)
LAST = NOW - timedelta(days=10)
SPAN = (FIRST, LAST)
KEPT = "reported_date is kept by the system"


def stage1(span=SPAN):
    return ctx(actor="stage1", span=span)


def cleanup(span=SPAN):
    return ctx(actor="stage2a", span=span)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {}, "type": "event", "expiry": EXPIRY_NONE,
    }
    base.update(over)
    return base


def seed(store, doc_id, reported, **entity):
    return put_raw(store, doc_id, doc_id, entity={"domain": "biographical", "reported_date": reported, **entity})


def iso(moment):
    return moment.isoformat()


def reported(store, fid):
    return store.docs[fid]["reported_date"]


async def create(store, context, **over):
    res = await store.adapter.create_fact("mother is Maria", meta(**over), context=context)
    assert res["status"] == "created", res
    return res


# ----------------------------------------------------------------------------------------------- create, conversation

@pytest.mark.parametrize("sent,expected", [
    (FIRST + timedelta(days=3), FIRST + timedelta(days=3)),
    (FIRST - timedelta(days=5), FIRST),
    (LAST + timedelta(days=3), LAST),
    (None, LAST),
], ids=["inside", "before the span", "after the span", "none sent"])
async def test_a_conversation_create_clamps_the_models_date_into_the_span_else_takes_its_end(store, sent, expected):
    over = {} if sent is None else {"reported_date": iso(sent)}
    res = await create(store, stage1(), **over)
    assert reported(store, res["fact_id"]) == expected
    assert "note" not in res


async def test_a_conversation_create_without_a_span_is_never_later_than_now(store):
    started = datetime.now(timezone.utc)
    future = await create(store, stage1(span=None), reported_date=iso(NOW + timedelta(days=5)))
    nothing = await create(store, stage1(span=None))
    for res in (future, nothing):
        assert started <= reported(store, res["fact_id"]) <= datetime.now(timezone.utc)


async def test_a_conversation_create_ignores_derived_from_with_a_note(store):
    seed(store, "src", NOW - timedelta(days=100))
    res = await create(store, stage1(), derived_from="src")
    assert reported(store, res["fact_id"]) == LAST
    assert "derived_from is ignored" in res["note"]


# ----------------------------------------------------------------------------------------------- create, cleanup

async def test_a_cleanup_create_ignores_the_models_date_and_says_so(store):
    res = await create(store, cleanup(), reported_date=iso(FIRST + timedelta(days=1)))
    assert reported(store, res["fact_id"]) == LAST
    assert res["note"].startswith(KEPT)


async def test_a_cleanup_create_that_sent_no_date_gets_no_note(store):
    res = await create(store, cleanup())
    assert reported(store, res["fact_id"]) == LAST
    assert "note" not in res


async def test_a_cleanup_create_without_a_span_or_sources_is_dated_now(store):
    started = datetime.now(timezone.utc)
    res = await create(store, cleanup(span=None))
    assert started <= reported(store, res["fact_id"]) <= datetime.now(timezone.utc)


async def test_a_cleanup_create_takes_the_date_of_the_fact_it_is_made_from(store):
    source_date = NOW - timedelta(days=40)
    seed(store, "src", source_date)
    res = await create(store, cleanup(), derived_from="src")
    assert reported(store, res["fact_id"]) == source_date
    assert "note" not in res


async def test_a_cleanup_create_from_several_sources_takes_the_latest_date(store):
    seed(store, "a", NOW - timedelta(days=60))
    seed(store, "b", NOW - timedelta(days=35))
    seed(store, "c", NOW - timedelta(days=50))
    res = await create(store, cleanup(), derived_from=["a", "b", "c"])
    assert reported(store, res["fact_id"]) == NOW - timedelta(days=35)


async def test_a_closed_source_still_lends_its_own_date(store):
    seed(store, "src", NOW - timedelta(days=45), state="superseded", is_current=False, superseded_by="elsewhere")
    res = await create(store, cleanup(), derived_from="src")
    assert reported(store, res["fact_id"]) == NOW - timedelta(days=45)


@pytest.mark.parametrize("named", ["no-such-fact", "foreign"])
async def test_a_foreign_or_unknown_source_falls_back_to_the_span_end_with_a_note(store, named):
    seed(store, "foreign", NOW - timedelta(days=45), account=OTHER)
    res = await create(store, cleanup(), derived_from=named)
    assert reported(store, res["fact_id"]) == LAST
    assert "named no readable fact" in res["note"]


async def test_a_readable_source_beats_an_unreadable_one_and_the_models_date_is_still_ignored(store):
    seed(store, "src", NOW - timedelta(days=40))
    res = await create(store, cleanup(), derived_from=["src", "no-such-fact"], reported_date=iso(FIRST))
    assert reported(store, res["fact_id"]) == NOW - timedelta(days=40)
    assert res["note"].startswith(KEPT)


# ----------------------------------------------------------------------------------------------- merge

async def merge(store, context, **over):
    seed(store, "m1", NOW - timedelta(days=80))
    seed(store, "m2", NOW - timedelta(days=30))
    res = await store.adapter.merge_facts(["m1", "m2"], "merged", meta(**over), context=context)
    assert res["status"] == "merged", res
    return res


@pytest.mark.parametrize("sent,expected", [
    (FIRST + timedelta(days=2), FIRST + timedelta(days=2)), (NOW, LAST), (None, LAST),
], ids=["inside", "after the span", "none sent"])
async def test_a_conversation_merge_clamps_the_models_date_else_takes_the_span_end(store, sent, expected):
    over = {} if sent is None else {"reported_date": iso(sent)}
    res = await merge(store, stage1(), **over)
    assert reported(store, res["new_fact_id"]) == expected
    assert "note" not in res


async def test_a_cleanup_merge_takes_the_latest_source_date_and_says_when_it_ignored_the_models(store):
    res = await merge(store, cleanup(), reported_date=iso(FIRST))
    assert reported(store, res["new_fact_id"]) == NOW - timedelta(days=30)
    assert res["note"].startswith(KEPT)


async def test_a_cleanup_merge_that_sent_no_date_gets_no_note(store):
    res = await merge(store, cleanup())
    assert reported(store, res["new_fact_id"]) == NOW - timedelta(days=30)
    assert "note" not in res


# ----------------------------------------------------------------------------------------------- version

STORED = NOW - timedelta(days=15)


async def new_version(store, context, **updates):
    seed(store, "v", STORED)
    res = await store.adapter.update_fact(
        "v", {"content": "a new text", "expiry": EXPIRY_NONE, **updates}, ACC, context=context)
    assert res["status"] == "updated" and res["fact_id"] != "v", res
    return res


@pytest.mark.parametrize("sent,expected", [
    (STORED + timedelta(days=2), STORED + timedelta(days=2)),
    (STORED - timedelta(days=5), STORED),
    (LAST + timedelta(days=4), LAST),
    (None, LAST),
], ids=["later", "earlier", "after the span", "none sent: the span end"])
async def test_a_conversation_version_takes_the_later_of_the_stored_and_the_clamped_date(store, sent, expected):
    updates = {} if sent is None else {"reported_date": iso(sent)}
    res = await new_version(store, stage1(), **updates)
    assert reported(store, res["fact_id"]) == expected


async def test_a_conversation_version_without_a_date_keeps_a_stored_date_later_than_the_span_end(store):
    seed(store, "new", NOW - timedelta(days=2))
    res = await store.adapter.update_fact(
        "new", {"content": "a new text", "expiry": EXPIRY_NONE}, ACC, context=stage1())
    assert reported(store, res["fact_id"]) == NOW - timedelta(days=2)


async def test_a_cleanup_version_keeps_the_stored_date_and_says_when_a_date_was_sent(store):
    sent = await new_version(store, cleanup(), reported_date=iso(LAST))
    assert reported(store, sent["fact_id"]) == STORED
    assert sent["note"].startswith(KEPT)

    plain = await new_version_of_another(store)
    assert reported(store, plain["fact_id"]) == STORED
    assert "note" not in plain


async def new_version_of_another(store):
    seed(store, "v2", STORED)
    return await store.adapter.update_fact("v2", {"content": "other", "expiry": EXPIRY_NONE}, ACC, context=cleanup())


# ----------------------------------------------------------------------------------------------- in place

@pytest.mark.parametrize("sent,expected", [
    (STORED + timedelta(days=2), STORED + timedelta(days=2)), (STORED - timedelta(days=3), STORED),
], ids=["later", "earlier"])
async def test_a_remention_in_a_conversation_pass_never_moves_the_date_back(store, sent, expected):
    seed(store, "p", STORED)
    res = await store.adapter.update_fact("p", {"reported_date": iso(sent)}, ACC, context=stage1())
    assert res["status"] == "updated" and res["fact_id"] == "p"
    assert reported(store, "p") == expected


async def test_an_in_place_update_without_a_date_leaves_it_alone(store):
    seed(store, "p", STORED)
    await store.adapter.update_fact("p", {"state": "archived"}, ACC, context=stage1())
    assert reported(store, "p") == STORED


async def test_a_cleanup_pass_cannot_move_the_date_of_a_fact_and_says_so(store):
    seed(store, "p", STORED)
    res = await store.adapter.update_fact("p", {"reported_date": iso(LAST)}, ACC, context=cleanup())
    assert res["status"] == "updated" and res["note"].startswith(KEPT)
    assert reported(store, "p") == STORED
