"""The expiry decision through `FactManagementAdapter` (Facts S3, T4a): create, merge, version and in-place writes store the
decision the code computes from `expiry`, and every refusal happens before anything is embedded or written.

Real adapter, write service and repository on the evaluating Firestore fake (index refusal ON); assertions are on the raw
stored documents and on the returned dicts.
"""
import copy
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.domain.fact_expiry import EXPIRY_IGNORED_ON_CLOSE
from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx, horizon, on

PARIS = ZoneInfo("Europe/Paris")
GENERIC_PIN_TEXT = "expiry is required for this fact"
CLEANUP = {"actor": "stage2a"}


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {"name": "Valentina"}, "type": "event",
    }
    base.update(over)
    return base


def days_ahead(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).astimezone(PARIS).date()


def end_of_day_utc(day):
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=PARIS).astimezone(timezone.utc)


def seed(store, doc_id, text="seeded", **entity):
    return put_raw(store, doc_id, text, entity={"domain": "biographical", **entity})


async def create(store, text="mother is Valentina", context=None, **over):
    res = await store.adapter.create_fact(text, meta(**over), context=context or ctx())
    assert res["status"] == "created", res
    return res["fact_id"]


def embedder_refuses(store, monkeypatch):
    async def fail(*_a, **_kw):
        raise AssertionError("the embedder must not be called before the refusal")

    monkeypatch.setattr(store.embedder, "get_embeddings_batch", fail)


# ----------------------------------------------------------------------------------------------- create

async def test_create_stores_a_date_decision_ending_at_the_owners_midnight(store):
    day = days_ahead(30)
    fid = await create(store, expiry=on(day))
    doc = store.docs[fid]
    assert (doc["expiry_kind"], doc["ttl_days"]) == ("date", None)
    assert doc["expires_at"] == end_of_day_utc(day)


async def test_create_stores_a_horizon_counted_from_reported_date_and_never_the_models_ttl(store):
    reported = datetime.now(timezone.utc) - timedelta(days=10)
    fid = await create(store, expiry=horizon(90), ttl_days=7, reported_date=reported.isoformat())
    doc = store.docs[fid]
    assert (doc["expiry_kind"], doc["ttl_days"]) == ("horizon", 90)
    assert doc["expires_at"] == reported + timedelta(days=90)


async def test_create_stores_none_as_nulls_and_a_model_ttl_is_dropped(store):
    fid = await create(store, expiry=EXPIRY_NONE, ttl_days=30)
    doc = store.docs[fid]
    assert (doc["expiry_kind"], doc["ttl_days"], doc["expires_at"]) == ("none", None, None)


async def test_create_without_expiry_is_refused_before_anything_is_embedded_or_written(store, monkeypatch):
    embedder_refuses(store, monkeypatch)
    res = await store.adapter.create_fact("mother is Valentina", meta(), context=ctx())
    assert (res["status"], res["refused"], res["fact_id"]) == ("failed", "expiry", None)
    assert "`fact_attributes`" in res["message"] and "Nothing was written" in res["message"]
    assert store.docs == {}


@pytest.mark.parametrize("raw", [None, "soon", {"kind": "horizon"}, {"kind": "date", "date": "tomorrow"}, 7])
async def test_create_with_a_malformed_expiry_is_refused_and_writes_nothing(store, monkeypatch, raw):
    embedder_refuses(store, monkeypatch)
    res = await store.adapter.create_fact("mother is Valentina", meta(expiry=raw), context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert "Nothing was written" in res["message"]
    assert store.docs == {}


async def test_the_generic_pin_text_never_reaches_a_tool_result(store):
    created = await create(store, expiry=EXPIRY_NONE)
    other = await create(store, "mother lives in Kyiv", expiry=EXPIRY_NONE)
    results = [
        await store.adapter.create_fact("a new fact", meta(), context=ctx()),
        await store.adapter.merge_facts([created, other], "merged", meta(), context=ctx()),
        await store.adapter.update_fact(created, {"content": "mother is Valentyna"}, ACC, context=ctx()),
    ]
    assert [r["refused"] for r in results] == ["expiry"] * 3
    assert all(GENERIC_PIN_TEXT not in r["message"] for r in results)
    assert "`fact_attributes`" in results[0]["message"] and "`fact_attributes`" in results[1]["message"]
    assert "`updates`" in results[2]["message"]


async def test_a_stage1_horizon_that_is_already_due_is_refused(store):
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    res = await store.adapter.create_fact(
        "mother is Valentina", meta(expiry=horizon(30), reported_date=old), context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert "give at least" in res["message"] and "Nothing was written" in res["message"]
    assert store.docs == {}


async def test_a_cleanup_horizon_that_is_already_due_is_floored_to_two_days_with_a_note(store):
    seed(store, "old-source", reported_date=datetime.now(timezone.utc) - timedelta(days=100))
    started = datetime.now(timezone.utc)
    res = await store.adapter.create_fact(
        "mother is Valentina", meta(expiry=horizon(30), derived_from="old-source"), context=ctx(**CLEANUP))
    assert res["status"] == "created"
    assert "had already passed" in res["note"]
    doc = store.docs[res["fact_id"]]
    assert (doc["expiry_kind"], doc["ttl_days"]) == ("horizon", 30)
    assert started + timedelta(days=2) <= doc["expires_at"] <= datetime.now(timezone.utc) + timedelta(days=2)


@pytest.mark.parametrize("context", [ctx(), ctx(**CLEANUP)], ids=["stage1", "cleanup"])
async def test_a_past_date_is_refused_on_every_kind_of_pass(store, context):
    res = await store.adapter.create_fact(
        "mother is Valentina", meta(expiry=on(days_ahead(-3))), context=context)
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert "is past" in res["message"]
    assert store.docs == {}


@pytest.mark.parametrize("over", [
    {"domain": "agent_directive"}, {"type": "principle"}, {"tags": ["mindset"]},
], ids=["directive", "principle", "mindset"])
async def test_a_pinned_fact_gets_none_without_an_expiry_and_refuses_any_other(store, over):
    fid = await create(store, **over)
    doc = store.docs[fid]
    assert (doc["expiry_kind"], doc["ttl_days"], doc["expires_at"]) == ("none", None, None)

    res = await store.adapter.create_fact("another", meta(expiry=horizon(30), **over), context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert "{kind: none}" in res["message"]
    assert list(store.docs) == [fid]


async def test_create_as_stale_is_refused(store):
    res = await store.adapter.create_fact("mother is Valentina", meta(state="stale", expiry=EXPIRY_NONE), context=ctx())
    assert res["status"] == "failed" and "STALE" in res["message"] and "refused" not in res
    assert store.docs == {}


@pytest.mark.parametrize("state", ["archived", "invalidated"])
async def test_a_fact_born_closed_needs_no_expiry_and_a_sent_past_date_is_ignored_with_a_note(store, caplog, state):
    with caplog.at_level(logging.INFO):
        plain = await store.adapter.create_fact("it happened", meta(state=state), context=ctx())
        sent = await store.adapter.create_fact(
            "it also happened", meta(state=state, expiry=on(days_ahead(-30))), context=ctx())
    assert plain["status"] == sent["status"] == "created"
    assert "note" not in plain and sent["note"] == EXPIRY_IGNORED_ON_CLOSE
    for res in (plain, sent):
        doc = store.docs[res["fact_id"]]
        assert (doc["expiry_kind"], doc["ttl_days"], doc["expires_at"]) == (None, None, None)
    assert any(EXPIRY_IGNORED_ON_CLOSE in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


# ----------------------------------------------------------------------------------------------- merge

async def test_merge_stores_the_decision_on_the_merged_fact_and_closes_the_sources(store):
    first = await create(store, "mother is Valentina", expiry=EXPIRY_NONE)
    second = await create(store, "mother lives in Kyiv", expiry=EXPIRY_NONE)
    day = days_ahead(20)
    res = await store.adapter.merge_facts([first, second], "mother Valentina lives in Kyiv", meta(expiry=on(day)),
                                          context=ctx())
    assert res["status"] == "merged"
    merged = store.docs[res["new_fact_id"]]
    assert (merged["expiry_kind"], merged["expires_at"]) == ("date", end_of_day_utc(day))
    assert {store.docs[f]["state"] for f in (first, second)} == {"superseded"}


@pytest.mark.parametrize("over", [{}, {"expiry": {"kind": "horizon"}}], ids=["missing", "malformed"])
async def test_merge_with_a_missing_or_malformed_expiry_is_refused_and_leaves_the_sources_current(store, monkeypatch, over):
    first = await create(store, "mother is Valentina", expiry=EXPIRY_NONE)
    second = await create(store, "mother lives in Kyiv", expiry=EXPIRY_NONE)
    before = copy.deepcopy(store.docs)
    embedder_refuses(store, monkeypatch)
    res = await store.adapter.merge_facts([first, second], "merged", meta(**over), context=ctx())
    assert (res["status"], res["refused"], res["new_fact_id"]) == ("failed", "expiry", None)
    assert store.docs == before


async def test_merge_into_a_pinned_fact_defaults_to_none(store):
    first = await create(store, "rule one", expiry=EXPIRY_NONE, domain="agent_directive")
    second = await create(store, "rule two", expiry=EXPIRY_NONE, domain="agent_directive")
    res = await store.adapter.merge_facts([first, second], "rule", meta(domain="agent_directive"),
                                          context=ctx(**CLEANUP))
    assert res["status"] == "merged" and store.docs[res["new_fact_id"]]["expiry_kind"] == "none"


async def test_a_cleanup_merge_horizon_that_is_due_is_floored_with_a_note(store):
    first = await create(store, "mother is Valentina", expiry=EXPIRY_NONE,
                         reported_date=(datetime.now(timezone.utc) - timedelta(days=200)).isoformat())
    second = await create(store, "mother lives in Kyiv", expiry=EXPIRY_NONE,
                          reported_date=(datetime.now(timezone.utc) - timedelta(days=150)).isoformat())
    res = await store.adapter.merge_facts([first, second], "merged", meta(expiry=horizon(30)), context=ctx(**CLEANUP))
    assert res["status"] == "merged" and "had already passed" in res["note"]
    assert store.docs[res["new_fact_id"]]["expires_at"] > datetime.now(timezone.utc) + timedelta(days=1)


async def test_merge_as_archived_needs_no_expiry(store):
    first = await create(store, "mother is Valentina", expiry=EXPIRY_NONE)
    second = await create(store, "mother lives in Kyiv", expiry=EXPIRY_NONE)
    res = await store.adapter.merge_facts([first, second], "merged", meta(state="archived", expiry=horizon(3)),
                                          context=ctx())
    assert res["status"] == "merged" and res["note"] == EXPIRY_IGNORED_ON_CLOSE
    assert store.docs[res["new_fact_id"]]["expiry_kind"] is None


# ----------------------------------------------------------------------------------------------- update

async def test_a_content_change_stores_the_decision_on_the_new_version(store):
    fid = await create(store, expiry=EXPIRY_NONE)
    day = days_ahead(15)
    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna", "expiry": on(day)}, ACC, context=ctx())
    assert res["status"] == "updated" and res["fact_id"] != fid
    version = store.docs[res["fact_id"]]
    assert (version["expiry_kind"], version["expires_at"]) == ("date", end_of_day_utc(day))
    assert store.docs[fid]["expiry_kind"] == "none"


@pytest.mark.parametrize("updates,field", [
    ({"content": "mother is Valentyna"}, "text"), ({"tags": ["family", "x"]}, "tags"),
    ({"temporal_class": "dynamic"}, "temporal_class"),
])
async def test_a_content_change_without_expiry_is_refused_and_writes_nothing(store, monkeypatch, updates, field):
    fid = await create(store, expiry=EXPIRY_NONE)
    before = copy.deepcopy(store.docs)
    embedder_refuses(store, monkeypatch)
    res = await store.adapter.update_fact(fid, updates, ACC, context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert f"`{field}`" in res["message"] and "`updates`" in res["message"]
    assert store.docs == before


async def test_a_pinned_fact_gets_none_on_a_new_version_without_expiry(store):
    fid = await create(store, expiry=EXPIRY_NONE, domain="agent_directive")
    res = await store.adapter.update_fact(fid, {"content": "a sharper rule"}, ACC, context=ctx(**CLEANUP))
    assert res["status"] == "updated" and store.docs[res["fact_id"]]["expiry_kind"] == "none"


async def test_an_expiry_alone_is_written_in_place(store):
    fid = await create(store, expiry=EXPIRY_NONE)
    day = days_ahead(25)
    res = await store.adapter.update_fact(fid, {"expiry": on(day)}, ACC, context=ctx())
    assert (res["status"], res["fact_id"], res["version"]) == ("updated", fid, 1)
    assert len(store.docs) == 1
    assert (store.docs[fid]["expiry_kind"], store.docs[fid]["expires_at"]) == ("date", end_of_day_utc(day))


async def test_an_expiry_alone_that_is_born_due_is_refused_in_place_too(store):
    fid = await create(store, expiry=EXPIRY_NONE)
    before = copy.deepcopy(store.docs)
    res = await store.adapter.update_fact(fid, {"expiry": on(days_ahead(-1))}, ACC, context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry") and "is past" in res["message"]
    assert store.docs == before


async def test_a_remention_moves_a_horizon_forward_and_never_back(store):
    reported = datetime.now(timezone.utc) - timedelta(days=5)
    fid = await create(store, expiry=horizon(30), reported_date=reported.isoformat())
    expires = store.docs[fid]["expires_at"]
    assert expires == reported + timedelta(days=30)

    earlier = (reported - timedelta(days=2)).isoformat()
    assert (await store.adapter.update_fact(fid, {"reported_date": earlier}, ACC, context=ctx()))["status"] == "updated"
    assert store.docs[fid]["expires_at"] == expires and store.docs[fid]["reported_date"] == reported

    later = datetime.now(timezone.utc) - timedelta(days=1)
    await store.adapter.update_fact(fid, {"reported_date": later.isoformat()}, ACC, context=ctx())
    assert store.docs[fid]["reported_date"] == later
    assert store.docs[fid]["expires_at"] == later + timedelta(days=30)


async def test_a_remention_never_moves_a_floored_cleanup_horizon_earlier(store):
    seed(store, "old-source", reported_date=datetime.now(timezone.utc) - timedelta(days=100))
    created = await store.adapter.create_fact(
        "mother is Valentina", meta(expiry=horizon(30), derived_from="old-source"), context=ctx(**CLEANUP))
    fid = created["fact_id"]
    floored = store.docs[fid]["expires_at"]
    old_batch = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    await store.adapter.update_fact(fid, {"reported_date": old_batch}, ACC, context=ctx())
    assert store.docs[fid]["expires_at"] == floored


async def test_stale_is_refused_from_the_model_on_update(store):
    fid = await create(store, expiry=EXPIRY_NONE)
    before = copy.deepcopy(store.docs)
    res = await store.adapter.update_fact(fid, {"state": "stale"}, ACC, context=ctx())
    assert res["status"] == "failed" and "only the system marks a fact stale" in res["message"]
    assert store.docs == before


async def test_an_expiry_sent_with_a_closing_state_is_ignored_with_a_note_and_a_log_line(store, caplog):
    fid = await create(store, expiry=horizon(60))
    decision = {k: store.docs[fid][k] for k in ("expiry_kind", "ttl_days", "expires_at")}
    with caplog.at_level(logging.INFO):
        res = await store.adapter.update_fact(fid, {"state": "archived", "expiry": on(days_ahead(-9))}, ACC,
                                              context=ctx())
    assert res["status"] == "updated" and res["note"] == EXPIRY_IGNORED_ON_CLOSE
    assert store.docs[fid]["state"] == "archived"
    assert {k: store.docs[fid][k] for k in decision} == decision
    assert any(EXPIRY_IGNORED_ON_CLOSE in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)


async def test_a_malformed_expiry_on_an_update_is_refused_before_the_embedding(store, monkeypatch):
    fid = await create(store, expiry=EXPIRY_NONE)
    before = copy.deepcopy(store.docs)
    embedder_refuses(store, monkeypatch)
    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna", "expiry": "soon"}, ACC,
                                          context=ctx())
    assert (res["status"], res["refused"]) == ("failed", "expiry")
    assert store.docs == before


async def test_a_not_found_fact_is_still_not_found_whatever_the_expiry(store):
    res = await store.adapter.update_fact("missing", {"expiry": "soon"}, ACC, context=ctx())
    assert res["status"] == "not_found"
    assert store.docs == {}
