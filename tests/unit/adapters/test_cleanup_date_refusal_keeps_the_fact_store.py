"""S3-D12 through the real adapter: each pass's context picks the refusal text a past `date` gets from `create_fact`."""
import pytest

from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, on

META = {
    "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
    "context_priority": "medium", "tags": ["trip"], "metadata": {}, "type": "event", "expiry": on("2020-01-01"),
}


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.mark.parametrize("actor", ["stage2a", "stage2b", "cluster", "backstop"])
async def test_a_cleanup_pass_is_told_to_keep_the_fact(store, actor):
    res = await store.adapter.create_fact("the trip", META, context=ctx(actor, model_dates=False))
    assert (res["status"], res["refused"], res["born_due"]) == ("failed", "expiry", True)
    assert "discard" not in res["message"].lower() and "do not drop the fact" in res["message"]
    assert not store.docs


@pytest.mark.parametrize("actor", ["stage1", "email"])
async def test_a_conversation_pass_may_still_discard_its_candidate(store, actor):
    res = await store.adapter.create_fact("the trip", META, context=ctx(actor))
    assert res["refused"] == "expiry" and "DISCARD the candidate" in res["message"]
