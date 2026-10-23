"""FactWriteService stores the decision fields the caller computed and no model `ttl_days` (Facts S3, plan T2).

Over the real service and repository on the evaluating Firestore fake, so the assertions read the stored documents.
"""

import dataclasses
from datetime import datetime, timezone

import pytest

from src.domain.fact_lifecycle import compute_expiry
from src.domain.new_fact_attributes import parse_new_fact
from tests.support.fact_kit import ACC, USER
from tests.support.fact_store import FactStore
from tests.support.fact_write import EXPIRY_NONE, horizon, on

REPORTED = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return FactStore()


async def write(store, **data):
    attrs = parse_new_fact({"text": "a fact", "reported_date": REPORTED.isoformat(), **data})
    if attrs.expiry is not None:
        attrs = dataclasses.replace(attrs, expiry_fields=compute_expiry(attrs.expiry, REPORTED, "Europe/Paris"))
    (entity,) = await store.write.build_facts(ACC, USER, [attrs])
    await store.repo.add_fact(entity)
    return store.docs[entity.id]


async def test_a_horizon_decision_is_stored_in_its_three_fields(store):
    doc = await write(store, expiry=horizon(14))
    assert (doc["expiry_kind"], doc["ttl_days"]) == ("horizon", 14)
    assert doc["expires_at"] == datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


async def test_a_date_decision_is_due_at_the_end_of_that_day_in_the_owners_zone(store):
    doc = await write(store, expiry=on("2026-10-30"))
    assert (doc["expiry_kind"], doc["ttl_days"]) == ("date", None)
    assert doc["expires_at"] == datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc)


async def test_a_none_decision_is_stored_as_none_with_no_expires_at(store):
    doc = await write(store, expiry=EXPIRY_NONE)
    assert (doc["expiry_kind"], doc["ttl_days"], doc["expires_at"]) == ("none", None, None)


async def test_a_fact_without_a_decision_stores_no_decision(store):
    doc = await write(store)
    assert (doc["expiry_kind"], doc["ttl_days"], doc["expires_at"]) == (None, None, None)


@pytest.mark.parametrize("ttl", [30, "soon", -1])
async def test_the_model_ttl_days_is_never_stored(store, ttl):
    doc = await write(store, ttl_days=ttl)
    assert doc["ttl_days"] is None and doc["expiry_kind"] is None


async def test_the_model_ttl_days_does_not_outrank_the_computed_horizon(store):
    doc = await write(store, ttl_days=999, expiry=horizon(14))
    assert doc["ttl_days"] == 14


async def test_add_facts_batch_the_v2_path_writes_facts_with_no_decision(store):
    data = [{"text": "a fact", "ttl_days": 90, "expiry": horizon(5)}]
    _, _, ids = await store.write.add_facts_batch(ACC, USER, data, skip_deduplication=True)
    doc = store.docs[ids[0]]
    assert (doc["ttl_days"], doc["expiry_kind"], doc["expires_at"]) == (None, None, None)
