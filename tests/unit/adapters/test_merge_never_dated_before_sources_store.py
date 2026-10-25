"""S3-D11 through `FactManagementAdapter.merge_facts` on the evaluating fake: the stored merged fact is dated no earlier
than its newest source, so a long horizon is not refused as born due from an old model date."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.support.fact_kit import ACC, USER, put_raw
from tests.support.fact_store import INDEXES_FILE, FactStore
from tests.support.fact_write import ctx, horizon

NOW = datetime.now(timezone.utc)
SPAN = (NOW - timedelta(days=80), NOW - timedelta(days=79))
FRESH = NOW - timedelta(days=4)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def seed(store, doc_id, reported):
    return put_raw(store, doc_id, doc_id, entity={"domain": "biographical", "reported_date": reported})


async def test_a_late_stage1_merge_keeps_the_fresher_source_date_and_a_60_day_horizon_is_accepted(store):
    seed(store, "old", NOW - timedelta(days=100))
    seed(store, "fresh", FRESH)
    meta = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family"], "metadata": {}, "type": "event",
        "expiry": horizon(60), "reported_date": SPAN[1].isoformat(),
    }
    res = await store.adapter.merge_facts(["old", "fresh"], "merged", meta, context=ctx("stage1", span=SPAN))
    assert res["status"] == "merged", res
    stored = store.docs[res["new_fact_id"]]
    assert stored["reported_date"] == FRESH
    assert stored["expires_at"] == FRESH + timedelta(days=60)
