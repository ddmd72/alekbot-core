"""The shared evaluating Firestore fake behaves like Firestore where repositories rely on it."""
import datetime

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from tests.support.firestore_eval import EvalDb, stored_vector

DESC = firestore.Query.DESCENDING


async def _seed(col, docs):
    for doc_id, data in docs.items():
        await col.document(doc_id).set(data)


def _ids(snaps):
    return [s.id for s in snaps]


async def test_find_nearest_without_result_field_and_vector_values():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"v": Vector([1.0, 0.0])}, "b": {"v": Vector([0.0, 1.0])}})
    snaps = await col.select(["v"]).find_nearest(
        vector_field="v", query_vector=Vector([1.0, 0.1]), distance_measure=DistanceMeasure.COSINE, limit=2
    ).get()
    assert _ids(snaps) == ["a", "b"]
    assert set(snaps[0].to_dict()) == {"v"}   # no distance field invented


async def test_find_nearest_adds_distance_field_when_asked():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"v": Vector([1.0, 0.0])}})
    snaps = await col.select(["v"]).find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=DistanceMeasure.COSINE, limit=1,
        distance_result_field="d",
    ).get()
    assert "d" not in snaps[0].to_dict()   # a projection that omits the distance field drops it
    full = await col.find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=DistanceMeasure.COSINE, limit=1,
        distance_result_field="d",
    ).get()
    assert full[0].to_dict()["d"] == pytest.approx(0.0)


async def test_find_nearest_honours_distance_threshold():
    col = EvalDb().collection("c")
    await _seed(col, {"near": {"v": Vector([1.0, 0.0])}, "far": {"v": Vector([0.0, 1.0])}})
    kw = dict(vector_field="v", query_vector=[1.0, 0.0], distance_measure=DistanceMeasure.COSINE, limit=5)
    assert _ids(await col.find_nearest(distance_threshold=0.04, **kw).get()) == ["near"]
    assert _ids(await col.find_nearest(distance_threshold=1.5, **kw).get()) == ["near", "far"]


async def test_find_nearest_skips_documents_without_the_vector_field():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"v": Vector([1.0, 0.0])}, "b": {"other": 1}})
    snaps = await col.find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=DistanceMeasure.COSINE, limit=5
    ).get()
    assert _ids(snaps) == ["a"]
    assert stored_vector(snaps[0].to_dict()["v"]) == [1.0, 0.0]


async def test_order_by_descending_and_missing_field_excluded():
    col = EvalDb().collection("c")
    t = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    await _seed(col, {
        "old": {"t": t},
        "new": {"t": t + datetime.timedelta(days=2)},
        "mid": {"t": t + datetime.timedelta(days=1)},
        "none": {"x": 1},
    })
    assert _ids(await col.order_by("t", direction=DESC).get()) == ["new", "mid", "old"]


async def test_in_filter_and_equality_excludes_missing_field():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"d": "x", "s": 1}, "b": {"d": "y", "s": 1}, "c": {"d": "z", "s": 1}, "m": {"s": 1}})
    snaps = await col.where(filter=FieldFilter("d", "in", ["x", "y"])).get()
    assert _ids(snaps) == ["a", "b"]
    assert _ids(await col.where(filter=FieldFilter("d", "==", "x")).get()) == ["a"]
    assert "m" not in _ids(await col.where(filter=FieldFilter("d", "==", None)).get())


async def test_select_drops_unselected_fields():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"keep": 1, "drop": 2}})
    snaps = await col.select(["keep"]).get()
    assert snaps[0].to_dict() == {"keep": 1}
    assert snaps[0].id == "a"


async def test_update_of_missing_document_raises_not_found():
    col = EvalDb().collection("c")
    with pytest.raises(NotFound):
        await col.document("ghost").update({"a": 1})
    assert "ghost" not in col.docs


async def test_update_merges_fields_into_existing_document():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"x": 1, "y": 2}})
    await col.document("a").update({"y": 3})
    assert col.docs["a"] == {"x": 1, "y": 3}


async def test_stream_yields_snapshots_asynchronously():
    col = EvalDb().collection("c")
    await _seed(col, {"a": {"x": 1}, "b": {"x": 2}})
    got = [s.id async for s in col.where(filter=FieldFilter("x", ">=", 1)).limit(1).stream()]
    assert got == ["a"]
