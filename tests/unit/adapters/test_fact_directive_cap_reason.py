"""Facts S3 · T4b (T4a review F1): the directive cap backstop's invalidation is recorded with the code-built reason
`directive cap`, never `model`; every other pass keeps `model`, and a superseded closure keeps its own reason."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import EXPIRY_NONE, ctx


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


async def seeded(store, text):
    meta = {
        "account_id": ACC, "user_id": USER, "domain": "agent_directive", "temporal_class": "stable",
        "context_priority": "medium", "tags": [], "metadata": {}, "type": "event", "expiry": EXPIRY_NONE,
    }
    res = await store.adapter.create_fact(text, meta, context=ctx(actor="stage1"))
    assert res["status"] == "created", res
    return res["fact_id"]


def last_event(store, fact_id):
    return store.docs[fact_id]["lifecycle_events"][-1]


async def test_the_backstop_invalidation_says_directive_cap(store):
    fid = await seeded(store, "never answer in verse")
    res = await store.adapter.update_fact(fid, {"state": "invalidated"}, ACC, context=ctx(actor="backstop", model_dates=False))
    assert res["status"] == "updated", res
    event = last_event(store, fid)
    assert (event["from"], event["to"], event["actor"], event["reason"]) == ("current", "invalidated", "backstop", "directive cap")


@pytest.mark.parametrize("actor", ["stage1", "stage2a", "stage2b", "email", "cluster"])
async def test_every_other_pass_keeps_the_model_reason(store, actor):
    fid = await seeded(store, f"rule for {actor}")
    await store.adapter.update_fact(fid, {"state": "invalidated"}, ACC, context=ctx(actor=actor, model_dates=actor in ("stage1", "email")))
    assert last_event(store, fid)["reason"] == "model"


async def test_a_superseded_closure_keeps_its_successor_reason_even_from_the_backstop(store):
    old, new = await seeded(store, "old rule"), await seeded(store, "new rule")
    res = await store.adapter.update_fact(
        old, {"state": "superseded", "superseded_by": new}, ACC, context=ctx(actor="backstop", model_dates=False))
    assert res["status"] == "updated", res
    assert last_event(store, old)["reason"] == f"superseded by {new}"
