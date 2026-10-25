"""The actor of the lifecycle trail is the pass that wrote, not a placeholder (Facts S3, T4a; T3 hand-off H1): a version
close, a merge close and an in-place state change record `context.actor` on the documents they close."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx

ACTORS = ["stage1", "stage2a", "stage2b", "backstop", "email", "cluster"]


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


def context(actor):
    return ctx(actor=actor)


async def seeded(store, text="mother is Maria"):
    res = await store.adapter.create_fact(text, meta(), context=context("stage1"))
    assert res["status"] == "created", res
    return res["fact_id"]


def trail(store, fact_id):
    return store.docs[fact_id]["lifecycle_events"]


@pytest.mark.parametrize("actor", ACTORS)
async def test_a_version_close_and_the_versions_own_entry_carry_the_passes_actor(store, actor):
    old = await seeded(store)
    res = await store.adapter.update_fact(old, {"content": "mother is Mariia", "expiry": EXPIRY_NONE}, ACC,
                                          context=context(actor))
    assert res["status"] == "updated", res
    new = res["fact_id"]
    closed = trail(store, old)[-1]
    assert (closed["from"], closed["to"], closed["actor"], closed["reason"]) == (
        "current", "superseded", actor, f"replaced by {new}")
    opened = trail(store, new)[-1]
    assert (opened["from"], opened["to"], opened["actor"], opened["reason"]) == (
        None, "current", actor, f"version 2 of {old}")


@pytest.mark.parametrize("actor", ACTORS)
async def test_a_merge_close_carries_the_passes_actor_on_every_source(store, actor):
    first = await seeded(store, "mother is Maria")
    second = await seeded(store, "mother lives in Springfield")
    res = await store.adapter.merge_facts([first, second], "merged", meta(), context=context(actor))
    assert res["status"] == "merged", res
    for source in (first, second):
        closed = trail(store, source)[-1]
        assert (closed["from"], closed["to"], closed["actor"]) == ("current", "superseded", actor)
        assert closed["reason"] == f"merged into {res['new_fact_id']}"
    assert trail(store, res["new_fact_id"]) == []


@pytest.mark.parametrize("actor", ACTORS)
async def test_an_in_place_state_change_carries_the_passes_actor(store, actor):
    fid = await seeded(store)
    res = await store.adapter.update_fact(fid, {"state": "invalidated"}, ACC, context=context(actor))
    assert res["status"] == "updated", res
    entry = trail(store, fid)[-1]
    reason = "directive cap" if actor == "backstop" else "model"     # plan §3.8: the backstop's code-built reason (T4a review F1)
    assert (entry["from"], entry["to"], entry["actor"], entry["reason"]) == ("current", "invalidated", actor, reason)


async def test_no_closure_is_written_under_the_old_placeholder_actor(store):
    old = await seeded(store, "mother is Maria")
    other = await seeded(store, "mother lives in Springfield")
    third = await seeded(store, "mother likes tea")
    await store.adapter.update_fact(old, {"content": "mother is Mariia", "expiry": EXPIRY_NONE}, ACC,
                                    context=context("stage2a"))
    await store.adapter.merge_facts([other, third], "merged", meta(), context=context("stage2b"))
    actors = {event["actor"] for doc in store.docs.values() for event in doc["lifecycle_events"]}
    assert actors == {"stage2a", "stage2b"}
