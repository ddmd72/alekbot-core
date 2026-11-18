"""The evaluating fake against what the test database answered in the P12 kit runs (tests/support/firestore_eval.py):
a stored timestamp reads back as a `DatetimeWithNanoseconds` in UTC, and, with `vector_dimension`, a vector query or a
stored vector of another dimension behaves as Firestore does (refused / not found)."""
from datetime import datetime, timedelta, timezone

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import FailedPrecondition
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.transforms import SERVER_TIMESTAMP
from google.cloud.firestore_v1.vector import Vector

from tests.support.firestore_eval import EvalDb


async def test_a_stored_timestamp_reads_back_as_datetime_with_nanoseconds_in_utc():
    db = EvalDb()
    ref = db.collection("jobs").document("j1")
    await ref.set({"naive": datetime(2026, 10, 2, 7, 0), "aware": datetime(2026, 10, 2, 9, 0, tzinfo=timezone(timedelta(hours=2))),
                   "nested": {"at": [datetime(2026, 10, 2, 7, 0, 0, 500000, tzinfo=timezone.utc)]}, "now": SERVER_TIMESTAMP})
    got = (await ref.get()).to_dict()
    for value in (got["naive"], got["aware"], got["nested"]["at"][0], got["now"]):
        assert type(value) is DatetimeWithNanoseconds and value.tzinfo == timezone.utc
    assert got["naive"] == got["aware"] == datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    assert got["nested"]["at"][0].microsecond == 500000


def nearest(db, query):
    return db.collection("tasks").find_nearest(vector_field="v", query_vector=Vector(query),
                                               distance_measure=DistanceMeasure.COSINE, limit=5)


async def test_with_a_vector_dimension_another_dimension_is_refused_or_not_found():
    db = EvalDb(vector_dimension=2)
    await db.collection("tasks").document("short").set({"v": Vector([1.0, 0.0])})
    await db.collection("tasks").document("long").set({"v": Vector([1.0, 0.0, 0.0])})
    assert [s.id for s in await nearest(db, [1.0, 0.0]).get()] == ["short"]
    with pytest.raises(FailedPrecondition, match="Missing vector index configuration"):
        await nearest(db, [1.0, 0.0, 0.0]).get()


async def test_without_a_vector_dimension_none_is_checked():
    db = EvalDb()
    await db.collection("tasks").document("short").set({"v": Vector([1.0, 0.0])})
    await db.collection("tasks").document("long").set({"v": Vector([1.0, 0.0, 0.0])})
    assert sorted(s.id for s in await nearest(db, [1.0, 0.0]).get()) == ["long", "short"]


async def test_several_vector_dimensions_accept_each_and_refuse_any_other():
    db = EvalDb(vector_dimension={2, 3})
    await db.collection("tasks").document("two").set({"v": Vector([1.0, 0.0])})
    await db.collection("tasks").document("three").set({"v": Vector([1.0, 0.0, 0.0])})
    await db.collection("tasks").document("four").set({"v": Vector([1.0, 0.0, 0.0, 0.0])})
    assert [s.id for s in await nearest(db, [1.0, 0.0]).get()] == ["two"]
    assert [s.id for s in await nearest(db, [1.0, 0.0, 0.0]).get()] == ["three"]
    with pytest.raises(FailedPrecondition, match="Missing vector index configuration"):
        await nearest(db, [1.0, 0.0, 0.0, 0.0]).get()
