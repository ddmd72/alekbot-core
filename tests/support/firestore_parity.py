"""Fake-vs-real parity scenarios (P5, TEST_COVERAGE_SYSTEMIC_RFC §5): one set of scenarios, three backends.

Each scenario is `async def scenario(db, col) -> result`: it writes into the collection `col`, reads back through `db`
(a `firestore.AsyncClient` or the H1 fake `EvalDb`) and returns ONE observable value (ids in order, field values, an
error class name). `SCENARIOS` pairs every function with its expected value: Firestore's documented behaviour, never
what a backend happens to answer. Every expected value was confirmed on the test database on 2026-10-22 (except the two
Facts S3 scenarios, range_timestamp_excludes_null_and_missing and projected_query_update_time_matches_transactional_read,
and the Facts S4 scenarios equality_and_in_without_composite and set_merge_fields_replaces_a_map_whole: documented
behaviour, not yet run on the test database, owner `make test-cloud` before deploy; and P11's
naive_datetime_filter_is_utc: the client's own encoding, run on the emulator 2026-10-27, not yet on the test database); where the
documentation is silent (`!=` and null or another type, equality plus array-contains, the error class of a precondition on
a deleted document, the order of several implicit inequality fields) that run is the source.

Backends: H1 (`tests/unit/support/test_firestore_parity.py`), the Firestore emulator (same file, when
`FIRESTORE_EMULATOR_HOST` is set), the test database (`tests/cloud/`). Index refusals are judged only where indexes are
enforced (H1 with declared indexes, the test database); the emulator does not refuse.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from google.api_core import exceptions as gexc
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.types.document import Value
from google.cloud.firestore_v1.vector import Vector

__all__ = ["Scenario", "SCENARIOS", "INDEX_GROUPS", "VECTOR_DIMENSION", "collection_name", "indexes_for", "purge"]

DESC = firestore.Query.DESCENDING
UTC = timezone.utc
VECTOR_DIMENSION = 3


# ------------------------------------------------------------------------------------------------- helpers

async def _ids(query: Any) -> List[str]:
    return [s.id async for s in query.stream()]


def _where(query: Any, field: str, op: str, value: Any) -> Any:
    return query.where(filter=FieldFilter(field, op, value))


async def _outcome(call: Awaitable[Any]) -> str:
    """"ok" when the call succeeds, else the class name of the error the backend raised."""
    try:
        await call
    except gexc.GoogleAPICallError as exc:
        return type(exc).__name__
    return "ok"


async def _ids_or_error(query: Any) -> Any:
    try:
        return await _ids(query)
    except gexc.GoogleAPICallError as exc:
        return type(exc).__name__


def _results(write_result: Any) -> List[Tuple[str, Any]]:
    """(kind, value) of each transform result: ("integer_value", 7), ("null_value", ...), ("timestamp_value", ...)."""
    kinds = [Value.pb(v).WhichOneof("value_type") for v in write_result.transform_results]
    return [(kind, getattr(v, kind)) for kind, v in zip(kinds, write_result.transform_results)]


def _kinds(write_result: Any) -> List[str]:
    return [kind for kind, _ in _results(write_result)]


async def _seed(col: Any, docs: Dict[str, Dict[str, Any]]) -> None:
    for doc_id, data in docs.items():
        await col.document(doc_id).set(data)


# ---------------------------------------------------------------------------------------------- ordering

# Values in Firestore's order; the document ids run the other way, so the name order never agrees with the value order.
_MIXED_IDS = [f"n{i:02d}" for i in range(11, 0, -1)]
_RARE_IDS = [f"r{i:02d}" for i in range(11, 0, -1)]


async def order_mixed_types(db: Any, col: str) -> Any:
    c = db.collection(col)
    values = [None, False, True, -5, 2.5, 3, datetime(2020, 1, 1, tzinfo=UTC), "B", "a", [1], {"k": 1}]
    await _seed(c, {doc_id: {"v": v} for doc_id, v in zip(_MIXED_IDS, values)})
    return {"asc": await _ids(c.order_by("v")), "desc": await _ids(c.order_by("v", direction=DESC))}


async def order_rare_types(db: Any, col: str) -> Any:
    c = db.collection(col)
    values = [None, float("nan"), 5, datetime(2020, 1, 1, tzinfo=UTC), "s", b"\x01", c.document("zz"), firestore.GeoPoint(1, 2),
              [1], Vector([1.0, 2.0]), {"k": 1}]
    await _seed(c, {doc_id: {"v": v} for doc_id, v in zip(_RARE_IDS, values)})
    return {"asc": await _ids(c.order_by("v")), "desc": await _ids(c.order_by("v", direction=DESC))}


async def order_ties_by_name(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"t2": {"w": 1}, "t1": {"w": 1}, "t3": {"w": 1}})
    return {"asc": await _ids(c.order_by("w")), "desc": await _ids(c.order_by("w", direction=DESC))}


async def range_implicit_order_limit(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 3}, "b": {"x": 1}, "c": {"x": 2}, "d": {"x": 1}, "e": {"y": 1}})
    return await _ids(_where(c, "x", ">", 0).limit(3))


async def _two_range_fields(db: Any, col: str, first: str, second: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 2, "y": 1}, "b": {"x": 1, "y": 2}, "c": {"x": 1, "y": 1}, "d": {"x": 2, "y": 2},
                    "e": {"x": 0, "y": 5}, "f": {"x": 5}})
    return await _ids(_where(_where(c, first, ">", 0), second, ">", 0))


async def two_range_fields_implicit_order(db: Any, col: str) -> Any:
    return await _two_range_fields(db, col, "x", "y")


async def two_range_fields_added_in_reverse(db: Any, col: str) -> Any:
    return await _two_range_fields(db, col, "y", "x")


# ---------------------------------------------------------------------------------------------- filters

async def missing_field_filters_orderings(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 1}, "b": {"x": None}, "c": {"y": 1}, "d": {"x": 2}})
    return {"order_by": await _ids(c.order_by("x")), "range": await _ids(_where(c, "x", ">", 0)),
            "is_null": await _ids(_where(c, "x", "==", None)), "in": await _ids(_where(c, "x", "in", [1, 2]))}


async def range_timestamp_excludes_null_and_missing(db: Any, col: str) -> Any:
    c = db.collection(col)
    early, late = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    await _seed(c, {"early": {"e": early}, "late": {"e": late}, "null": {"e": None}, "missing": {"y": 1},
                    "text": {"e": "2026-01-01"}, "number": {"e": 5}})
    cut = datetime(2026, 3, 1, tzinfo=UTC)
    return {"le": await _ids(_where(c, "e", "<=", cut)), "gt": await _ids(_where(c, "e", ">", cut)),
            "le_far_future": await _ids(_where(c, "e", "<=", datetime(2100, 1, 1, tzinfo=UTC)))}


async def naive_datetime_filter_is_utc(db: Any, col: str) -> Any:
    """A naive filter value is the UTC instant (the client's `encode_value`), whatever the machine's zone."""
    c = db.collection(col)
    await _seed(c, {"t10": {"e": datetime(2026, 1, 1, 10, tzinfo=UTC)}, "t12": {"e": datetime(2026, 1, 1, 12, tzinfo=UTC)},
                    "t14": {"e": datetime(2026, 1, 1, 14, tzinfo=UTC)}})
    return {"ge": await _ids(_where(c, "e", ">=", datetime(2026, 1, 1, 11))),
            "le": await _ids(_where(c, "e", "<=", datetime(2026, 1, 1, 12))),
            "eq": await _ids(_where(c, "e", "==", datetime(2026, 1, 1, 12))),
            "in": await _ids(_where(c, "e", "in", [datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 14)]))}


async def not_equal_missing_field(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 1}, "c": {"y": 1}, "d": {"x": 2}})
    return await _ids(_where(c, "x", "!=", 1))


async def not_equal_null_field(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 1}, "b": {"x": None}, "d": {"x": 2}})
    return await _ids(_where(c, "x", "!=", 1))


async def not_equal_other_type(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 1}, "f": {"x": 1.0}, "s": {"x": "1"}, "d": {"x": 2}})
    return await _ids(_where(c, "x", "!=", 1))


async def equality_array_contains(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"k": "x", "tags": ["p", "q"]}, "b": {"k": "x", "tags": ["r"]}, "c": {"k": "y", "tags": ["p"]}})
    return await _ids(_where(_where(c, "k", "==", "x"), "tags", "array_contains", "p"))


async def equality_array_contains_any(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"k": "x", "tags": ["p", "q"]}, "b": {"k": "x", "tags": ["r"]}, "c": {"k": "y", "tags": ["p"]},
                    "d": {"k": "x", "tags": ["z"]}})
    return await _ids(_where(_where(c, "k", "==", "x"), "tags", "array_contains_any", ["p", "r"]))


async def equality_order_by_missing_field(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"u": 1, "t": 2}, "b": {"u": 1}, "c": {"u": 1, "t": 1}, "d": {"u": 2, "t": 0}})
    return await _ids(_where(c, "u", "==", 1).order_by("t"))


async def equality_and_in_without_composite(db: Any, col: str) -> Any:
    """The review store's reads: an equality next to an `in` on another field, and four equalities, with no composite index."""
    c = db.collection(col)
    await _seed(c, {
        "a": {"account_id": "x", "status": "queued", "platform": "slack", "channel": "C1", "message_id": "m1"},
        "b": {"account_id": "x", "status": "closed", "platform": "slack", "channel": "C1", "message_id": "m2"},
        "c": {"account_id": "y", "status": "asked", "platform": "slack", "channel": "C1", "message_id": "m1"},
        "d": {"account_id": "x", "status": "asked", "platform": "telegram", "channel": "C1", "message_id": "m1"},
        "e": {"account_id": "x", "status": "unanswered"}})
    in_query = _where(_where(c, "account_id", "==", "x"), "status", "in", ["queued", "asked", "unanswered"])
    four = _where(_where(_where(_where(c, "account_id", "==", "x"), "platform", "==", "slack"), "channel", "==", "C1"),
                  "message_id", "==", "m1")
    # membership only: the claim is "no composite needed"; the order of an `in` result without `order_by` is not documented
    # and the review store orders in code (plan §3.13)
    results = {"in": await _ids_or_error(in_query), "four_equalities": await _ids_or_error(four)}
    return {k: sorted(v) if isinstance(v, list) else v for k, v in results.items()}


# ----------------------------------------------------------------------------------------- dotted paths

async def dotted_paths(db: Any, col: str) -> Any:
    c = db.collection(col)
    await c.document("a").set({"ids": {"slack": "U1"}, "usage": {"n": 1}})
    await c.document("b").set({"usage": {"n": 5}})
    await c.document("a").update({"ids.tg": "T1", "usage.n": 2})
    snap = await c.document("a").get()
    return {"doc": snap.to_dict(), "get": snap.get("usage.n"),
            "where": await _ids(_where(c, "ids.tg", "==", "T1")),
            "order_by": await _ids(c.order_by("usage.n", direction=DESC)),
            "select": {s.id: s.to_dict() async for s in c.select(["usage.n"]).stream()}}


async def set_dotted_key_is_literal(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"p.q": 1})
    return (await d.get()).to_dict()


# ------------------------------------------------------------------------------------- stored transforms

async def increment_results(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"usage": {"z": 1, "a": 1.5, "m": "text"}})
    result = await d.update({"usage.z": firestore.Increment(2), "usage.a": firestore.Increment(1),
                             "usage.m": firestore.Increment(7), "usage.new": firestore.Increment(4)})
    return {"stored": (await d.get()).to_dict(),
            "results": _results(result)}


async def array_union_remove(db: Any, col: str) -> Any:
    d = db.collection(col).document("w")
    await d.set({"e": firestore.ArrayUnion(["a"])}, merge=True)
    union = await d.set({"e": firestore.ArrayUnion(["b", "a"])}, merge=True)
    after_union = (await d.get()).to_dict()["e"]
    remove = await d.set({"e": firestore.ArrayRemove(["a", "zzz"])}, merge=True)
    return {"after_union": after_union, "after_remove": (await d.get()).to_dict()["e"],
            "union_result": _kinds(union), "remove_result": _kinds(remove)}


async def server_timestamp(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"u": firestore.SERVER_TIMESTAMP, "m": {"t": firestore.SERVER_TIMESTAMP}}, merge=True)
    result = await d.update({"u2": firestore.SERVER_TIMESTAMP})
    got = (await d.get()).to_dict()
    return {"u_is_aware_datetime": isinstance(got["u"], datetime) and got["u"].tzinfo is not None,
            "nested_is_datetime": isinstance(got["m"]["t"], datetime),
            "update_not_before_set": got["u2"] >= got["u"], "update_result": _kinds(result)}


# ------------------------------------------------------------------------------------ writes, preconditions

async def create_existing_document(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.create({"v": 1})
    second = await _outcome(d.create({"v": 2}))
    return {"second_create": second, "kept": (await d.get()).to_dict()["v"]}


async def update_missing_document(db: Any, col: str) -> Any:
    return await _outcome(db.collection(col).document("nobody").update({"a": 1}))


async def update_precondition_stale_fresh(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"v": 1})
    first = await d.get()
    await d.update({"v": 2})
    stale = await _outcome(d.update({"v": 3}, option=db.write_option(last_update_time=first.update_time)))
    second = await d.get()
    fresh = await _outcome(d.update({"v": 4}, option=db.write_option(last_update_time=second.update_time)))
    return {"stale": stale, "fresh": fresh, "v": (await d.get()).to_dict()["v"]}


async def update_precondition_deleted(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"v": 1})
    snap = await d.get()
    await d.delete()
    return await _outcome(d.update({"v": 2}, option=db.write_option(last_update_time=snap.update_time)))


async def delete_missing_document(db: Any, col: str) -> Any:
    c = db.collection(col)
    d = c.document("nobody")
    outcome = await _outcome(d.delete())
    return {"delete": outcome, "exists": (await d.get()).exists, "ids": await _ids(c)}


async def set_merge_fields_replaces_a_map_whole(db: Any, col: str) -> Any:
    """The review store's state marks: `set(..., merge=[fields])` replaces each listed field whole, a map too (a removed
    key is gone), keeps the fields not listed, and creates a missing document with the listed fields only. `merge=True`
    merges maps key by key, for contrast."""
    c = db.collection(col)
    seed = {"account_id": "x", "sent": 3, "offers": {"f1": 2, "f2": 1},
            "labels": {"uk": {"a": "1", "b": "2"}, "en": {"a": "A"}}}
    change = {"account_id": "x", "offers": {"f2": 1}, "labels": {"uk": {"a": "9"}}}
    await c.document("fields").set(seed)
    await c.document("fields").set(change, merge=["account_id", "offers", "labels"])
    after_fields = (await c.document("fields").get()).to_dict()
    await c.document("fields").set({"offers": {}}, merge=["offers"])
    await c.document("true").set(seed)
    await c.document("true").set(change, merge=True)
    await c.document("missing").set(change, merge=["account_id", "offers"])
    return {"merge_fields": after_fields, "last_key_removed": (await c.document("fields").get()).to_dict()["offers"],
            "merge_true": (await c.document("true").get()).to_dict(),
            "created": (await c.document("missing").get()).to_dict()}


async def set_replaces_whole_document(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"a": 1, "b": 2, "m": {"x": 1, "y": 2}})
    await d.set({"b": 3, "m": {"x": 9}})
    return (await d.get()).to_dict()


async def aware_datetime_stored_as_utc(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    sent = datetime(2026, 10, 22, 15, 30, 45, 123456, tzinfo=timezone(timedelta(hours=3)))
    await d.set({"t": sent})
    got = (await d.get()).to_dict()["t"]
    return {"utc_fields": (got.year, got.month, got.day, got.hour, got.minute, got.second, got.microsecond),
            "offset": got.utcoffset(), "same_instant": got == sent}


async def document_ids_are_case_sensitive(db: Any, col: str) -> Any:
    c = db.collection(col)
    await c.document("Doc").set({"v": 1})
    await c.document("doc").set({"v": 2})
    return {"ids": sorted(await _ids(c)), "Doc": (await c.document("Doc").get()).to_dict(),
            "doc": (await c.document("doc").get()).to_dict()}


# ----------------------------------------------------------------------------------------------- vectors

QUERY_VECTOR = [1.0, 0.0, 0.0]
# id: (user, status, n, vector). Cosine distance to QUERY_VECTOR: d1 0, d4 .0012, d2 .0061, d6 .2929, d3 1.0.
VECTOR_DOCS: Dict[str, Tuple[str, Optional[str], int, Optional[List[float]]]] = {
    "d1": ("u1", "open", 1, [1.0, 0.0, 0.0]), "d2": ("u1", "done", 2, [0.9, 0.1, 0.0]),
    "d3": ("u1", "open", 3, [0.0, 1.0, 0.0]), "d4": ("u2", "open", 4, [1.0, 0.05, 0.0]),
    "d5": ("u1", "open", 5, None), "d6": ("u1", None, 6, [0.7, 0.7, 0.0]),
}


async def _seed_vectors(col: Any) -> None:
    for doc_id, (user, status, n, vector) in VECTOR_DOCS.items():
        data: Dict[str, Any] = {"user": user, "n": n}
        if status is not None:
            data["status"] = status
        if vector is not None:
            data["v"] = Vector(vector)
        await col.document(doc_id).set(data)


async def _nearest(query: Any, field: str = "v", limit: int = 10, **kw: Any) -> List[Any]:
    return await query.find_nearest(vector_field=field, query_vector=Vector(QUERY_VECTOR), limit=limit,
                                    distance_measure=DistanceMeasure.COSINE, **kw).get()


async def _nearest_ids(query: Any, **kw: Any) -> List[str]:
    return [s.id for s in await _nearest(query, **kw)]


async def knn_cosine(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    snaps = await _nearest(c, distance_result_field="dist")
    return {"all": [[s.id, round(s.to_dict()["dist"], 4)] for s in snaps], "limit_2": await _nearest_ids(c, limit=2)}


async def knn_distance_threshold(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    asked = await _nearest(c, distance_threshold=0.1, distance_result_field="dist")
    not_asked = await _nearest(c, distance_threshold=0.1)
    return {"threshold_0.1": [s.id for s in asked], "dist_present_when_asked": all("dist" in s.to_dict() for s in asked),
            "dist_present_when_not_asked": any("dist" in s.to_dict() for s in not_asked),
            "threshold_1.0": await _nearest_ids(c, distance_threshold=1.0)}


async def knn_prefilter_equality(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return await _nearest_ids(_where(c, "user", "==", "u1"))


async def knn_prefilter_range(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return await _nearest_ids(_where(c, "n", ">", 2))


async def knn_prefilter_not_equal(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return await _nearest_ids(_where(c, "status", "!=", "done"))


async def knn_prefilter_in(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return await _nearest_ids(_where(c, "user", "in", ["u2", "zz"]))


async def knn_task_search_shape(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return await _nearest_ids(_where(_where(c, "user", "==", "u1"), "status", "!=", "done"), limit=20,
                              distance_threshold=1.0)


async def knn_projection_distance_field(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    both = await _nearest(c.select(["n", "dist"]), limit=1, distance_result_field="dist")
    only_n = await _nearest(c.select(["n"]), limit=1, distance_result_field="dist")
    return {"selected_with_dist": sorted(both[0].to_dict()), "selected_without_dist": sorted(only_n[0].to_dict())}


async def knn_refused_without_index(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed_vectors(c)
    return {"indexed_field": await _nearest_ids(c),
            "field_without_index": await _outcome(_nearest(c, field="w")),
            "prefilter_without_index": await _outcome(_nearest(_where(c, "other", "==", 1)))}


# ----------------------------------------------------------------------------------------- index refusals

async def composite_refused_without_index(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"u": 1, "t": 2}, "b": {"u": 1, "t": 1}, "c": {"u": 2, "t": 1}})
    return {"indexed": await _ids_or_error(_where(c, "u", "==", 1).order_by("t")),
            "order_by_field_without_index": await _ids_or_error(_where(c, "u", "==", 1).order_by("q")),
            "fields_swapped": await _ids_or_error(_where(c, "t", "==", 1).order_by("u"))}


async def composite_not_served_in_reverse(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"x": 1, "y": 1}, "b": {"x": 1, "y": 2}, "c": {"x": 2, "y": 1}})
    return {"declared_directions": await _ids_or_error(c.order_by("x").order_by("y")),
            "every_direction_flipped": await _ids_or_error(c.order_by("x", direction=DESC).order_by("y", direction=DESC)),
            "mixed_directions": await _ids_or_error(c.order_by("x").order_by("y", direction=DESC))}


# ------------------------------------------------------------------------------------------- transactions

async def transactions_concurrent(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"n": 0})
    runs = {"n": 0}

    @firestore.async_transactional
    async def bump(tx: Any) -> None:
        runs["n"] += 1
        snap = await d.get(transaction=tx)
        await asyncio.sleep(0.2)     # both transactions hold their read before the first one writes
        tx.update(d, {"n": snap.get("n") + 1})

    async def start_later(delay: float) -> None:
        await asyncio.sleep(delay)   # offset the starts: two transactions in lock step deadlock the emulator again and again
        await bump(db.transaction())

    await asyncio.gather(start_later(0), start_later(0.05))
    return {"final": (await d.get()).to_dict()["n"], "body_ran_again": runs["n"] > 2}


async def transactions_concurrent_create_on_missing(db: Any, col: str) -> Any:
    # Only the outcome is guaranteed: the test database sometimes makes the loser's read wait for the winner's commit
    # (pessimistic locking) instead of aborting and re-running its body (2 of 11 runs, 2026-10-23), so no run count.
    d = db.collection(col).document("lease")

    @firestore.async_transactional
    async def take(tx: Any) -> str:
        snap = await d.get(transaction=tx)
        await asyncio.sleep(0.2)     # both transactions hold their read of the missing document before the first writes
        if snap.exists:
            return "saw"
        tx.set(d, {"owner": "x"})
        return "created"

    async def start_later(delay: float) -> str:
        await asyncio.sleep(delay)
        return await take(db.transaction())

    results = await asyncio.gather(start_later(0), start_later(0.05))
    return {"results": sorted(results), "stored": (await d.get()).to_dict()}


async def transaction_read_missing_document(db: Any, col: str) -> Any:
    d = db.collection(col).document("nobody")

    @firestore.async_transactional
    async def read_only(tx: Any) -> bool:
        return (await d.get(transaction=tx)).exists

    seen = await read_only(db.transaction())
    return {"exists_in_transaction": seen, "created": (await d.get()).exists}


async def transaction_delete_after_read(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"v": 1})

    @firestore.async_transactional
    async def consume(tx: Any) -> Any:
        snap = await d.get(transaction=tx)
        tx.delete(d)
        return snap.to_dict()

    @firestore.async_transactional
    async def read_again(tx: Any) -> bool:
        return (await d.get(transaction=tx)).exists

    first = await consume(db.transaction())
    return {"first_read": first, "exists_after": (await d.get()).exists,
            "second_transaction_sees": await read_again(db.transaction())}


async def transaction_read_update_time(db: Any, col: str) -> Any:
    d = db.collection(col).document("a")
    await d.set({"v": 1})
    other = db.collection(col).document("b")
    await other.set({"v": 1})
    plain = await d.get()

    @firestore.async_transactional
    async def read_in_transaction(tx: Any) -> Any:
        return (await d.get(transaction=tx)).update_time

    inside = await read_in_transaction(db.transaction())
    await d.update({"v": 2})
    after = await d.get()
    inside_after = await read_in_transaction(db.transaction())
    return {"equal_to_plain_read": inside == plain.update_time, "differs_after_write": after.update_time != plain.update_time,
            "transaction_sees_new": inside_after == after.update_time,
            "separate_writes_differ": plain.update_time != (await other.get()).update_time}


async def projected_query_update_time_matches_transactional_read(db: Any, col: str) -> Any:
    c = db.collection(col)
    d = c.document("a")
    await d.set({"v": 1, "w": 2})
    await _seed(c, {"b": {"v": 1, "w": 2}})

    @firestore.async_transactional
    async def read_in_transaction(tx: Any) -> Any:
        return (await d.get(transaction=tx)).update_time

    projected = {s.id: s async for s in c.select(["v"]).stream()}
    inside = await read_in_transaction(db.transaction())
    full = await d.get()
    return {"projected_fields": sorted(projected["a"].to_dict()),
            "projected_equals_transactional": projected["a"].update_time == inside,
            "projected_equals_plain_read": projected["a"].update_time == full.update_time,
            "documents_differ": projected["a"].update_time != projected["b"].update_time}


async def get_all_order_not_assumed(db: Any, col: str) -> Any:
    c = db.collection(col)
    await _seed(c, {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}})
    refs = [c.document(i) for i in ("c", "a", "nobody", "b")]

    @firestore.async_transactional
    async def read_each(tx: Any) -> Dict[str, Any]:
        return {snap.id: snap.to_dict() for snap in [await ref.get(transaction=tx) for ref in refs]}

    return {"get_all": {snap.id: snap.to_dict() async for snap in db.get_all(refs)},
            "in_transaction": await read_each(db.transaction())}


# ------------------------------------------------------------------------------------ the table, indexes

@dataclass(frozen=True)
class Scenario:
    name: str
    run: Callable[[Any, str], Awaitable[Any]]
    expected: Any
    group: Optional[str] = None     # index group: the scenario runs in that group's fixed collection on the test database
    refuses: bool = False           # judges an index refusal: runs only where indexes are enforced


_ALL_VECTORS = ["d1", "d4", "d2", "d6", "d3"]       # by cosine distance; d5 has no vector

SCENARIOS: List[Scenario] = [
    Scenario("order_mixed_types", order_mixed_types, {"asc": _MIXED_IDS, "desc": _MIXED_IDS[::-1]}),
    Scenario("order_rare_types", order_rare_types, {"asc": _RARE_IDS, "desc": _RARE_IDS[::-1]}),
    Scenario("order_ties_by_name", order_ties_by_name, {"asc": ["t1", "t2", "t3"], "desc": ["t3", "t2", "t1"]}),
    Scenario("range_implicit_order_limit", range_implicit_order_limit, ["b", "d", "c"]),
    Scenario("two_range_fields_implicit_order", two_range_fields_implicit_order, ["c", "b", "a", "d"], group="composite"),
    Scenario("two_range_fields_added_in_reverse", two_range_fields_added_in_reverse, ["c", "b", "a", "d"], group="composite"),
    Scenario("missing_field_filters_orderings", missing_field_filters_orderings,
             {"order_by": ["b", "a", "d"], "range": ["a", "d"], "is_null": ["b"], "in": ["a", "d"]}),
    Scenario("range_timestamp_excludes_null_and_missing", range_timestamp_excludes_null_and_missing,
             {"le": ["early"], "gt": ["late"], "le_far_future": ["early", "late"]}),
    Scenario("naive_datetime_filter_is_utc", naive_datetime_filter_is_utc,
             {"ge": ["t12", "t14"], "le": ["t10", "t12"], "eq": ["t12"], "in": ["t10", "t14"]}),
    Scenario("not_equal_missing_field", not_equal_missing_field, ["d"]),
    Scenario("not_equal_null_field", not_equal_null_field, ["d"]),
    Scenario("not_equal_other_type", not_equal_other_type, ["d", "s"]),
    Scenario("equality_array_contains", equality_array_contains, ["a"]),
    Scenario("equality_array_contains_any", equality_array_contains_any, ["a", "b"]),
    Scenario("equality_and_in_without_composite", equality_and_in_without_composite,
             {"in": ["a", "d", "e"], "four_equalities": ["a"]}),
    Scenario("equality_order_by_missing_field", equality_order_by_missing_field, ["c", "a"], group="composite"),
    Scenario("dotted_paths", dotted_paths, {
        "doc": {"ids": {"slack": "U1", "tg": "T1"}, "usage": {"n": 2}}, "get": 2, "where": ["a"], "order_by": ["b", "a"],
        "select": {"a": {"usage": {"n": 2}}, "b": {"usage": {"n": 5}}}}),
    Scenario("set_dotted_key_is_literal", set_dotted_key_is_literal, {"p.q": 1}),
    Scenario("increment_results", increment_results, {
        "stored": {"usage": {"z": 3, "a": 2.5, "m": 7, "new": 4}},
        "results": [("double_value", 2.5), ("integer_value", 7), ("integer_value", 4), ("integer_value", 3)]}),
    Scenario("array_union_remove", array_union_remove, {
        "after_union": ["a", "b"], "after_remove": ["b"], "union_result": ["null_value"], "remove_result": ["null_value"]}),
    Scenario("server_timestamp", server_timestamp, {
        "u_is_aware_datetime": True, "nested_is_datetime": True, "update_not_before_set": True,
        "update_result": ["timestamp_value"]}),
    Scenario("create_existing_document", create_existing_document, {"second_create": "AlreadyExists", "kept": 1}),
    Scenario("update_missing_document", update_missing_document, "NotFound"),
    Scenario("update_precondition_stale_fresh", update_precondition_stale_fresh,
             {"stale": "FailedPrecondition", "fresh": "ok", "v": 4}),
    Scenario("update_precondition_deleted", update_precondition_deleted, "FailedPrecondition"),
    Scenario("delete_missing_document", delete_missing_document, {"delete": "ok", "exists": False, "ids": []}),
    Scenario("set_replaces_whole_document", set_replaces_whole_document, {"b": 3, "m": {"x": 9}}),
    Scenario("set_merge_fields_replaces_a_map_whole", set_merge_fields_replaces_a_map_whole, {
        "merge_fields": {"account_id": "x", "sent": 3, "offers": {"f2": 1}, "labels": {"uk": {"a": "9"}}},
        "last_key_removed": {},
        "merge_true": {"account_id": "x", "sent": 3, "offers": {"f1": 2, "f2": 1},
                       "labels": {"uk": {"a": "9", "b": "2"}, "en": {"a": "A"}}},
        "created": {"account_id": "x", "offers": {"f2": 1}}}),
    Scenario("aware_datetime_stored_as_utc", aware_datetime_stored_as_utc, {
        "utc_fields": (2026, 10, 22, 12, 30, 45, 123456), "offset": timedelta(0), "same_instant": True}),
    Scenario("document_ids_are_case_sensitive", document_ids_are_case_sensitive, {
        "ids": ["Doc", "doc"], "Doc": {"v": 1}, "doc": {"v": 2}}),
    Scenario("knn_cosine", knn_cosine, {
        "all": [["d1", 0.0], ["d4", 0.0012], ["d2", 0.0061], ["d6", 0.2929], ["d3", 1.0]], "limit_2": ["d1", "d4"]},
        group="knn"),
    Scenario("knn_distance_threshold", knn_distance_threshold, {
        "threshold_0.1": ["d1", "d4", "d2"], "dist_present_when_asked": True, "dist_present_when_not_asked": False,
        "threshold_1.0": _ALL_VECTORS}, group="knn"),
    Scenario("knn_prefilter_equality", knn_prefilter_equality, ["d1", "d2", "d6", "d3"], group="knn"),
    Scenario("knn_prefilter_range", knn_prefilter_range, ["d4", "d6", "d3"], group="knn"),
    Scenario("knn_prefilter_not_equal", knn_prefilter_not_equal, ["d1", "d4", "d3"], group="knn"),
    Scenario("knn_prefilter_in", knn_prefilter_in, ["d4"], group="knn"),
    Scenario("knn_task_search_shape", knn_task_search_shape, ["d1", "d3"], group="knn"),
    Scenario("knn_projection_distance_field", knn_projection_distance_field,
             {"selected_with_dist": ["dist", "n"], "selected_without_dist": ["n"]}, group="knn"),
    Scenario("knn_refused_without_index", knn_refused_without_index, {
        "indexed_field": _ALL_VECTORS, "field_without_index": "FailedPrecondition",
        "prefilter_without_index": "FailedPrecondition"}, group="knn", refuses=True),
    Scenario("composite_refused_without_index", composite_refused_without_index, {
        "indexed": ["b", "a"], "order_by_field_without_index": "FailedPrecondition", "fields_swapped": "FailedPrecondition"},
        group="composite", refuses=True),
    Scenario("composite_not_served_in_reverse", composite_not_served_in_reverse,
             {"declared_directions": ["a", "b", "c"], "every_direction_flipped": "FailedPrecondition",
              "mixed_directions": "FailedPrecondition"},
             group="composite", refuses=True),
    Scenario("transactions_concurrent", transactions_concurrent, {"final": 2, "body_ran_again": True}),
    Scenario("transactions_concurrent_create_on_missing", transactions_concurrent_create_on_missing,
             {"results": ["created", "saw"], "stored": {"owner": "x"}}),
    Scenario("transaction_read_missing_document", transaction_read_missing_document,
             {"exists_in_transaction": False, "created": False}),
    Scenario("transaction_delete_after_read", transaction_delete_after_read,
             {"first_read": {"v": 1}, "exists_after": False, "second_transaction_sees": False}),
    Scenario("transaction_read_update_time", transaction_read_update_time,
             {"equal_to_plain_read": True, "differs_after_write": True, "transaction_sees_new": True,
              "separate_writes_differ": True}),
    Scenario("projected_query_update_time_matches_transactional_read",
             projected_query_update_time_matches_transactional_read,
             {"projected_fields": ["v"], "projected_equals_transactional": True, "projected_equals_plain_read": True,
              "documents_differ": True}),
    Scenario("get_all_order_not_assumed", get_all_order_not_assumed, {
        "get_all": {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}, "nobody": None},
        "in_transaction": {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}, "nobody": None}}),
]

# Fields of each group's composite and vector indexes, in index order: (field, "ASC" | "VECTOR").
INDEX_GROUPS: Dict[str, List[List[Tuple[str, str]]]] = {
    "composite": [[("u", "ASC"), ("t", "ASC")], [("x", "ASC"), ("y", "ASC")]],
    "knn": [[("v", "VECTOR")], [("user", "ASC"), ("v", "VECTOR")], [("n", "ASC"), ("v", "VECTOR")],
            [("status", "ASC"), ("v", "VECTOR")], [("user", "ASC"), ("status", "ASC"), ("v", "VECTOR")]],
}


def collection_name(scenario: Scenario, run_id: str, fixed: bool = False) -> str:
    """The collection a scenario writes to. `fixed` (the test database) gives a group's scenarios its group's name,
    because a composite index belongs to a collection id and is created once."""
    if fixed and scenario.group:
        return f"test_parity_idx_{scenario.group}"
    return f"test_parity_{run_id}_{scenario.name}"


def indexes_for(scenario: Scenario, col: str) -> List[Dict[str, Any]]:
    """The scenario's indexes on `col`, in the shape of `config/firestore.indexes.json` (what `EvalDb(indexes=...)` takes)."""
    def field(path: str, mode: str) -> Dict[str, Any]:
        if mode == "VECTOR":
            return {"fieldPath": path, "vectorConfig": {"dimension": VECTOR_DIMENSION, "flat": {}}}
        return {"fieldPath": path, "order": "ASCENDING"}

    return [{"collectionGroup": col, "queryScope": "COLLECTION", "fields": [field(p, m) for p, m in fields]}
            for fields in INDEX_GROUPS.get(scenario.group or "", [])]


async def purge(db: Any, col: str) -> None:
    """Delete every document the scenario left in `col`."""
    async for ref in db.collection(col).list_documents():
        await ref.delete()
