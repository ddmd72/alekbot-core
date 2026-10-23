"""Facts S3 · T14 done-check: merge refuses a missing expiry with the `expiry` tag, and a fact born closed carries the
resolved `reported_date` (create and merge)."""
from datetime import datetime, timezone

import pytest

from tests.support.fact_store import FactStore, INDEXES_FILE
from tests.support.fact_write import ctx
from tests.unit.adapters.test_fact_management_kit import create, meta


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def at(day: int, hour: int = 10) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


async def test_a_merge_of_a_current_fact_without_expiry_is_refused_with_the_expiry_tag_and_writes_nothing(store):
    source = await create(store, "likes tea")
    before = dict(store.docs)
    attributes = meta()
    attributes.pop("expiry")

    res = await store.adapter.merge_facts([source], "likes tea daily", attributes, context=ctx())

    assert res["status"] == "failed" and res["refused"] == "expiry" and "born_due" not in res
    assert "merge_facts needs `expiry`" in res["message"]
    assert store.docs == before


async def test_a_fact_born_closed_by_create_carries_the_end_of_the_message_span_as_its_reported_date(store):
    attributes = meta(state="archived")
    attributes.pop("expiry")

    res = await store.adapter.create_fact("mother is Valentina", attributes, context=ctx(span=(at(3), at(4, 18))))

    assert res["status"] == "created"
    assert store.docs[res["fact_id"]]["reported_date"] == at(4, 18)


async def test_a_fact_born_closed_by_a_cleanup_merge_carries_the_latest_source_date_as_its_reported_date(store):
    older = await create(store, "likes tea", reported_date="2026-03-02T09:00:00Z")
    newer = await create(store, "likes green tea", reported_date="2026-03-05T09:00:00Z")
    attributes = meta(state="invalidated")
    attributes.pop("expiry")

    res = await store.adapter.merge_facts(
        [older, newer], "likes tea daily", attributes, context=ctx(actor="stage2a", model_dates=False))

    assert res["status"] == "merged"
    assert store.docs[res["new_fact_id"]]["reported_date"] == at(5, 9)


async def test_a_current_fact_with_an_expiry_carries_the_end_of_the_message_span_as_its_reported_date(store):
    res = await store.adapter.create_fact("mother is Valentina", meta(), context=ctx(span=(at(3), at(4, 18))))

    assert res["status"] == "created"
    assert store.docs[res["fact_id"]]["reported_date"] == at(4, 18)
