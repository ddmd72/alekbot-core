"""H1 of TEST_COVERAGE_SYSTEMIC_RFC §3: the shared Firestore fake behaves like Firestore for what the repositories use.

One block per add-list item: positive case, and the refusal where Firestore has one. Assertions are on stored data and
on query results.
"""
import asyncio
import datetime

import pytest
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, InvalidArgument, NotFound
from google.cloud import firestore
from google.cloud.firestore import FieldFilter
from google.cloud.firestore_v1 import ReadAfterWriteError
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from tests.support.firestore_eval import EvalDb, eval_transactions

COSINE = DistanceMeasure.COSINE
DESC = firestore.Query.DESCENDING


@pytest.fixture
def db():
    return EvalDb()


def _ids(snaps):
    return [s.id for s in snaps]


# ------------------------------------------------------------------------------------------ dotted field paths

async def test_update_treats_dotted_keys_as_nested_paths(db):
    col = db.collection("accounts")
    col.docs["a"] = {"usage": {"total": 1, "daily": 5}, "name": "x"}
    await col.document("a").update({"usage.total": 4, "usage.new.deep": True, "usage.daily": firestore.DELETE_FIELD})
    assert col.docs["a"] == {"usage": {"total": 4, "new": {"deep": True}}, "name": "x"}


async def test_dotted_paths_in_where_order_by_select_and_nested_reads(db):
    col = db.collection("users")
    col.docs["u1"] = {"identity": {"slack": "S1", "rank": 2}, "n": 1}
    col.docs["u2"] = {"identity": {"slack": "S2", "rank": 1}, "n": 2}
    col.docs["u3"] = {"identity": {"rank": 3}, "n": 3}                      # no identity.slack
    assert _ids(await col.where("identity.slack", "==", "S2").get()) == ["u2"]
    assert _ids(await col.order_by("identity.rank").get()) == ["u2", "u1", "u3"]
    assert _ids(await col.order_by("identity.slack").get()) == ["u1", "u2"]   # an ordering drops the document without the field
    snaps = await col.select(["identity.slack"]).get()
    assert [s.to_dict() for s in snaps] == [{"identity": {"slack": "S1"}}, {"identity": {"slack": "S2"}}, {}]
    snap = await col.document("u1").get(field_paths=["identity.rank"])
    assert snap.to_dict() == {"identity": {"rank": 2}} and snap.get("identity.rank") == 2
    with pytest.raises(KeyError):
        snap.get("identity.slack")
    assert (await col.document("nobody").get()).get("identity.rank") is None      # the client: None for a missing document


# -------------------------------------------------------------------------------- WriteResult.transform_results

async def test_update_returns_increment_values_after_the_transform_in_path_order(db):
    col = db.collection("accounts")
    col.docs["a"] = {"usage": {"cost": 1.5, "tokens": 10}}
    result = await col.document("a").update({
        "usage.tokens": firestore.Increment(5), "usage.cost": firestore.Increment(0.25), "usage.requests": firestore.Increment(1),
    })
    values = list(result.transform_results)
    # alphabetical by path (cost, requests, tokens), not insertion order; an int stays an integer value
    assert (values[0].double_value, values[1].integer_value, values[2].integer_value) == (1.75, 1, 15)
    assert col.docs["a"]["usage"] == {"cost": 1.75, "tokens": 15, "requests": 1}


async def test_set_returns_a_result_too_and_a_write_without_transforms_has_none(db):
    ref = db.collection("c").document("d")
    plain = await ref.set({"n": 1})
    assert list(plain.transform_results) == []
    counted = await ref.set({"n": firestore.Increment(2)}, merge=True)
    assert [v.integer_value for v in counted.transform_results] == [3]


# ------------------------------------------------------------------------------------------- positional where

async def test_positional_where_next_to_filter_keyword(db):
    col = db.collection("c")
    col.docs.update({"a": {"k": 1, "t": "x"}, "b": {"k": 2, "t": "x"}, "c": {"k": 2, "t": "y"}})
    assert _ids(await col.where("k", "==", 2).get()) == ["b", "c"]
    assert _ids(await col.where("k", "==", 2).where(filter=FieldFilter("t", "==", "y")).get()) == ["c"]
    assert _ids(await col.where(filter=FieldFilter("k", ">=", 2)).where("t", "in", ["x"]).get()) == ["b"]
    with pytest.raises(AssertionError):
        col.where()


# --------------------------------------------------------------------------- ArrayUnion / ArrayRemove / SERVER_TIMESTAMP

async def test_array_union_and_remove_are_applied(db):
    col = db.collection("whitelist")
    col.docs["cfg"] = {"emails": ["a@x", "b@x"], "domains": ["x.org"]}
    ref = col.document("cfg")
    await ref.update({"emails": firestore.ArrayUnion(["b@x", "c@x"]), "domains": firestore.ArrayRemove(["x.org", "none"])})
    assert col.docs["cfg"] == {"emails": ["a@x", "b@x", "c@x"], "domains": []}
    await ref.set({"emails": firestore.ArrayRemove(["a@x"]), "fresh": firestore.ArrayUnion([1, 2])}, merge=True)
    assert col.docs["cfg"] == {"emails": ["b@x", "c@x"], "domains": [], "fresh": [1, 2]}


async def test_server_timestamp_is_stored_as_a_tz_aware_utc_datetime(db):
    col = db.collection("c")
    before = datetime.datetime.now(datetime.timezone.utc)
    await col.document("a").set({"at": firestore.SERVER_TIMESTAMP, "nested": {"at": firestore.SERVER_TIMESTAMP}})
    await col.document("a").update({"later": firestore.SERVER_TIMESTAMP})
    doc = col.docs["a"]
    for stamp in (doc["at"], doc["nested"]["at"], doc["later"]):
        assert isinstance(stamp, datetime.datetime) and stamp.utcoffset() == datetime.timedelta(0)
        assert before <= stamp <= datetime.datetime.now(datetime.timezone.utc)
    assert doc["at"] <= doc["later"]


# ----------------------------------------------------------------------------------------------------- create

async def test_create_refuses_an_existing_document_and_keeps_it(db):
    ref = db.collection("c").document("d")
    await ref.create({"v": 1, "at": firestore.SERVER_TIMESTAMP})
    assert db.collection("c").docs["d"]["v"] == 1 and "at" in db.collection("c").docs["d"]
    with pytest.raises(AlreadyExists):
        await ref.create({"v": 2})
    assert db.collection("c").docs["d"]["v"] == 1


async def test_update_of_a_missing_document_raises_not_found_with_dotted_keys_too(db):
    with pytest.raises(NotFound):
        await db.collection("c").document("ghost").update({"a.b": 1})
    assert db.collection("c").docs == {}


# ------------------------------------------------------------------------------------------- atomic commits

async def test_a_batch_with_a_failing_write_applies_nothing(db):
    col = db.collection("c")
    col.docs.update({"exists": {"v": 1}, "other": {"v": 1}})
    before_version = dict(col.versions)
    for failing in (
        lambda b: b.update(col.document("ghost"), {"v": 1}),                                     # NotFound
        lambda b: b.update(col.document("other"), {"v": 2}, option=db.write_option(last_update_time=99)),   # precondition
        lambda b: b.delete(col.document("other"), option=db.write_option(last_update_time=99)),
    ):
        batch = db.batch()
        batch.set(col.document("fresh"), {"v": 1})
        batch.update(col.document("exists"), {"v": firestore.Increment(5)})
        batch.delete(col.document("other"))
        failing(batch)
        with pytest.raises((NotFound, FailedPrecondition)):
            await batch.commit()
        assert col.docs == {"exists": {"v": 1}, "other": {"v": 1}} and col.versions == before_version
    assert db.commits == []
    ok = db.batch()
    ok.set(col.document("fresh"), {"v": 1})
    await ok.commit()
    assert "fresh" in col.docs and len(db.commits) == 1


async def test_a_transaction_commit_with_a_failing_write_applies_nothing_and_is_not_retried(db):
    col = db.collection("c")
    col.docs["taken"] = {"v": 1}
    runs, reader_runs = [], []
    with eval_transactions():
        @firestore.async_transactional
        async def body(tx):
            runs.append(1)
            tx.set(col.document("fresh"), {"v": 1})
            tx.set(col.document("taken"), {"v": 2})
            tx.update(col.document("ghost"), {"v": 3})                                           # NotFound at commit

        with pytest.raises(NotFound):
            await body(db.transaction())
        assert runs == [1] and col.docs == {"taken": {"v": 1}}

        # the failed commit counts as no commit: a transaction that read `taken` before it still commits, no re-run
        @firestore.async_transactional
        async def reader(tx):
            reader_runs.append(1)
            await col.document("taken").get(transaction=tx)
            if len(reader_runs) == 1:
                with pytest.raises(NotFound):
                    await body(db.transaction())
            tx.set(col.document("out"), {"v": 1})

        await reader(db.transaction())
    assert reader_runs == [1] and col.docs["out"] == {"v": 1} and col.docs["taken"] == {"v": 1}


# --------------------------------------------------------------------------------------- value encoding

async def test_values_are_stored_as_the_client_encodes_them(db):
    col = db.collection("c")
    naive = datetime.datetime(2026, 1, 1, 12, 0, 0)
    plus3 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))
    await col.document("a").set({"naive": naive, "aware": plus3, "tuple": (1, 2), "set": {7}, "nested": {"t": (3,)}})
    doc = col.docs["a"]
    assert doc["naive"] == datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
    assert doc["aware"] == datetime.datetime(2026, 1, 1, 9, 0, tzinfo=datetime.timezone.utc)
    assert doc["aware"].utcoffset() == datetime.timedelta(0)
    assert (doc["tuple"], doc["set"], doc["nested"]) == ([1, 2], [7], {"t": [3]})
    await col.document("a").update({"naive": datetime.datetime(2026, 2, 2)})
    assert col.docs["a"]["naive"].tzinfo is datetime.timezone.utc
    with pytest.raises(TypeError, match="Cannot convert to a Firestore Value"):
        await col.document("b").set({"day": datetime.date(2026, 1, 1)})
    with pytest.raises(TypeError):
        await col.document("a").update({"x.y": [datetime.date(2026, 1, 1)]})
    assert "b" not in col.docs and "x" not in col.docs["a"]


# ------------------------------------------------------------- subcollections, auto-id, list_documents, groups

async def test_subcollections_are_separate_collections_under_the_document(db):
    skills = db.collection("skills")
    files = skills.document("u1:plan").collection("files")
    await files.document("sha1").set({"content": "x"})
    await skills.document("u1:other").collection("files").document("sha2").set({"content": "y"})
    assert files.name == "skills/u1:plan/files" and db.collection("skills/u1:plan/files") is files
    assert skills.docs == {}                                    # the parent document does not exist, the children do
    assert [r.id async for r in files.list_documents()] == ["sha1"]
    assert (await files.document("sha1").get()).to_dict() == {"content": "x"}
    assert not (await skills.document("u1:plan").collection("files").document("sha2").get()).exists


async def test_raw_dict_seeding_of_a_subcollection(db):
    db.collection("skills/p/versions").docs["v1"] = {"n": 1}
    assert _ids(await db.collection("skills").document("p").collection("versions").get()) == ["v1"]


async def test_document_without_id_gets_a_fresh_20_character_id(db):
    col = db.collection("prompts")
    one, two = col.document(), col.document()
    assert one.id != two.id and len(one.id) == 20 and one.id.isalnum()
    await one.set({"v": 1})
    assert list(col.docs) == [one.id]


async def test_collection_group_spans_every_parent_and_the_top_level(db):
    db.collection("windows").docs["top"] = {"user_id": "u", "index": 5}
    db.collection("jobs/j1/windows").docs["a"] = {"user_id": "u", "index": 1}
    db.collection("jobs/j2/windows").docs["b"] = {"user_id": "u", "index": 3}
    db.collection("jobs/j2/windows").docs["c"] = {"user_id": "other", "index": 2}
    db.collection("jobs/j2/parts").docs["d"] = {"user_id": "u", "index": 0}
    snaps = await db.collection_group("windows").where("user_id", "==", "u").order_by("index").get()
    assert _ids(snaps) == ["a", "b", "top"]
    assert [s.reference.col.name for s in snaps] == ["jobs/j1/windows", "jobs/j2/windows", "windows"]
    await snaps[0].reference.delete()                           # the reference is the real one
    assert "a" not in db.collection("jobs/j1/windows").docs


# -------------------------------------------------------------------------------------------------------- !=

async def test_not_equal_excludes_documents_without_the_field(db):
    col = db.collection("tasks")
    col.docs.update({
        "open": {"status": "open"}, "done": {"status": "completed"}, "none": {"status": None}, "missing": {"x": 1},
        "num": {"status": 3},
    })
    got = await col.where(filter=FieldFilter("status", "!=", "completed")).get()
    # Firestore docs: a null field is excluded too; P5 verifies: a value of another type is "not equal"
    assert _ids(got) == ["num", "open"]
    assert _ids(await col.where("status", "!=", None).get()) == ["num", "done", "open"]       # IS_NOT_NULL, ordered by the field (number before string)
    assert _ids(await col.where(filter=FieldFilter("status", "!=", None)).get()) == ["num", "done", "open"]


async def test_none_is_valid_only_with_equality_or_not_equal_like_the_client(db):
    col = db.collection("tasks")
    with pytest.raises(ValueError, match="Only equality"):
        col.where("status", "<", None)
    with pytest.raises(ValueError, match="Only equality"):
        col.where("status", "in", None)


async def test_a_range_or_not_equal_filter_orders_by_its_field_so_a_limit_keeps_the_smallest(db):
    col = db.collection("c")
    col.docs.update({"a": {"n": 30}, "b": {"n": 10}, "c": {"n": 20}, "d": {"n": 5}, "e": {"x": 1}})
    # by document id the first two over 4 would be a, b; the implicit ordering by `n` gives the two smallest
    assert _ids(await col.where("n", ">", 4).limit(2).get()) == ["d", "b"]
    assert _ids(await col.where("n", "!=", 10).limit(2).get()) == ["d", "c"]
    # several implicit inequality fields follow the explicit ones sorted by field path, whatever the filters' order
    many = db.collection("m")
    many.docs.update({"p": {"a": 2, "b": 1}, "q": {"a": 1, "b": 2}, "r": {"a": 1, "b": 1}})
    assert _ids(await many.where("b", ">", 0).where("a", ">", 0).get()) == ["r", "q", "p"]      # by a, then b
    # after an explicit descending order the implicit field follows in that direction (then __name__)
    ties = db.collection("t")
    ties.docs.update({"y": {"k": 1, "n": 5}, "x": {"k": 1, "n": 2}, "z": {"k": 0, "n": 3}})
    assert _ids(await ties.order_by("k", direction=DESC).where("n", ">", 0).get()) == ["y", "x", "z"]


# ------------------------------------------------------------------- stream(transaction=), get_all(transaction=)

async def test_stream_and_get_all_accept_a_transaction(db):
    col = db.collection("c")
    col.docs.update({"a": {"g": 1}, "b": {"g": 1}, "c": {"g": 2}})
    tx = db.transaction()
    assert _ids([s async for s in col.where("g", "==", 1).stream(transaction=tx)]) == ["a", "b"]
    snaps = [s async for s in db.get_all([col.document("c"), col.document("nope")], field_paths=["g"], transaction=tx)]
    assert [(s.id, s.exists) for s in snaps] == [("c", True), ("nope", False)]
    assert _ids(await col.where("g", "==", 2).get(transaction=tx)) == ["c"]


async def _read_with_stream(tx, col):
    return [s async for s in col.where("g", "==", 1).stream(transaction=tx)]


async def _read_with_get_all(tx, col):
    return [s async for s in tx.db.get_all([col.document("a")], transaction=tx)]


async def _read_with_query_get(tx, col):
    return await col.where("g", "==", 1).get(transaction=tx)


@pytest.mark.parametrize("read", [_read_with_stream, _read_with_get_all, _read_with_query_get])
async def test_a_rival_commit_to_a_document_read_through_a_query_or_get_all_re_runs_the_body(db, read):
    col = db.collection("c")
    col.docs.update({"a": {"g": 1}, "out": {"v": 0}})
    runs, rival_pending = [], [True]
    with eval_transactions():
        @firestore.async_transactional
        async def rival(tx):
            tx.update(col.document("a"), {"g": 1, "by": "rival"})

        @firestore.async_transactional
        async def body(tx):
            runs.append(1)
            await read(tx, col)
            if rival_pending:
                rival_pending.pop()
                await rival(db.transaction())
            tx.set(col.document("out"), {"v": len(runs)})

        await body(db.transaction())
    assert len(runs) == 2 and col.docs["out"] == {"v": 2}


async def test_a_rival_commit_to_a_document_that_was_not_read_does_not_re_run_the_body(db):
    col = db.collection("c")
    col.docs.update({"a": {"g": 1}, "other": {"g": 9}})
    runs = []
    with eval_transactions():
        @firestore.async_transactional
        async def rival(tx):
            tx.update(col.document("other"), {"g": 10})

        @firestore.async_transactional
        async def body(tx):
            runs.append(1)
            await _read_with_stream(tx, col)
            await rival(db.transaction())
            tx.set(col.document("out"), {"v": 1})

        await body(db.transaction())
    assert runs == [1]


# ---------------------------------------------------------------------------------------------- transactions

async def test_the_patch_is_global_and_removed_afterwards(db):
    original = firestore.async_transactional
    with eval_transactions():
        assert firestore.async_transactional is not original
    assert firestore.async_transactional is original


async def test_transaction_writes_are_buffered_until_commit_and_applied_in_order(db):
    col = db.collection("c")
    col.docs["old"] = {"v": 1}
    seen_inside = {}
    with eval_transactions():
        @firestore.async_transactional
        async def body(tx):
            snap = await col.document("old").get(transaction=tx)
            tx.create(col.document("new"), {"v": snap.to_dict()["v"] + 1})
            tx.update(col.document("old"), {"v": firestore.Increment(10)})
            tx.set(col.document("set"), {"v": 0})
            tx.delete(col.document("old"))
            seen_inside.update({k: dict(v) for k, v in col.docs.items()})
            return "done"

        assert await body(db.transaction()) == "done"
    assert seen_inside == {"old": {"v": 1}}                      # nothing applied while the body ran
    assert col.docs == {"new": {"v": 2}, "set": {"v": 0}}


async def test_a_read_after_a_write_raises_and_nothing_is_applied(db):
    col = db.collection("c")
    col.docs["a"] = {"v": 1}
    with eval_transactions():
        @firestore.async_transactional
        async def read_after_set(tx):
            tx.set(col.document("a"), {"v": 2})
            await col.document("a").get(transaction=tx)

        @firestore.async_transactional
        async def stream_after_set(tx):
            tx.delete(col.document("a"))
            return [s async for s in col.stream(transaction=tx)]

        with pytest.raises(ReadAfterWriteError):
            await read_after_set(db.transaction())
        with pytest.raises(ReadAfterWriteError):
            await stream_after_set(db.transaction())
    assert col.docs == {"a": {"v": 1}}


async def test_an_error_in_the_body_discards_its_writes_and_is_not_retried(db):
    col = db.collection("c")
    runs = []
    with eval_transactions():
        @firestore.async_transactional
        async def body(tx):
            runs.append(1)
            tx.set(col.document("a"), {"v": 1})
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await body(db.transaction())
    assert col.docs == {} and runs == [1]


async def test_a_commit_aborted_by_another_transaction_re_runs_the_body(db):
    col = db.collection("c")
    col.docs["n"] = {"v": 0}
    ref, seen, interfere = col.document("n"), [], [True]
    with eval_transactions():
        @firestore.async_transactional
        async def increment(tx):
            v = (await ref.get(transaction=tx)).to_dict()["v"]
            seen.append(v)
            if interfere:
                interfere.pop()
                await increment(db.transaction())              # commits first, from the same snapshot
            tx.set(ref, {"v": v + 1})

        await increment(db.transaction())
    assert col.docs["n"] == {"v": 2}                            # no lost update
    assert seen == [0, 0, 1]                                    # the retry re-read the document


async def test_a_transaction_that_keeps_aborting_fails_with_the_clients_error_after_five_attempts(db):
    col = db.collection("c")
    col.docs["n"] = {"v": 0}
    ref, runs = col.document("n"), []
    with eval_transactions():
        @firestore.async_transactional
        async def rival(tx):
            tx.set(ref, {"v": -1})

        @firestore.async_transactional
        async def loser(tx):
            runs.append(1)
            await ref.get(transaction=tx)
            await rival(db.transaction())
            tx.set(ref, {"v": 99})

        with pytest.raises(ValueError, match="5 attempts"):
            await loser(db.transaction())
    assert len(runs) == 5 and col.docs["n"] == {"v": -1}


async def test_an_outside_write_during_the_body_does_not_retry(db):
    col = db.collection("c")
    col.docs["n"] = {"v": 0}
    ref, runs = col.document("n"), []
    with eval_transactions():
        @firestore.async_transactional
        async def body(tx):
            runs.append(1)
            await ref.get(transaction=tx)
            await ref.set({"v": 50})                           # real Firestore would block this write until commit
            tx.set(ref, {"v": 1})

        await body(db.transaction())
    # only what is true of real Firestore: the body does not run again. (Real Firestore blocks the outside write
    # until the transaction ends, so the final value is not asserted.)
    assert runs == [1]


async def test_a_real_repository_transaction_serialises_two_consumers(db):
    store = FirestoreEphemeralStore(db, "tickets")
    await store.set("t", {"persona": "x"}, ttl_s=60)
    db.collection("tickets").yield_on_get = True               # both read the document before either commits
    with eval_transactions():
        got = await asyncio.gather(store.get_and_delete("t"), store.get_and_delete("t"))
    assert sorted(got, key=lambda v: v is None) == [{"persona": "x"}, None]
    assert db.collection("tickets").docs == {}


# ------------------------------------------------------------------------------------------------- find_nearest

def _vectors(db):
    col = db.collection("facts")
    col.docs.update({
        "near": {"acc": "A", "rank": 1, "v": Vector([1.0, 0.0])},
        "mid": {"acc": "A", "rank": 5, "v": Vector([1.0, 1.0])},
        "other": {"acc": "B", "rank": 9, "v": Vector([1.0, 0.1])},
    })
    return col


async def test_find_nearest_refuses_any_measure_but_cosine(db):
    col = _vectors(db)
    base = dict(vector_field="v", query_vector=[1.0, 0.0], limit=3)
    assert _ids(await col.find_nearest(distance_measure=COSINE, **base).get()) == ["near", "other", "mid"]
    for measure in (DistanceMeasure.EUCLIDEAN, DistanceMeasure.DOT_PRODUCT):
        with pytest.raises(AssertionError, match="COSINE"):
            await col.find_nearest(distance_measure=measure, **base).get()
    with pytest.raises(AssertionError, match="COSINE"):
        await col.find_nearest(**base).get()


async def test_find_nearest_prefilter_may_be_a_range_or_not_equal(db):
    col = _vectors(db)
    kw = dict(vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=3)
    assert _ids(await col.where("rank", ">", 1).find_nearest(**kw).get()) == ["other", "mid"]
    assert _ids(await col.where("acc", "!=", "B").find_nearest(**kw).get()) == ["near", "mid"]
    assert _ids(await col.where("acc", "==", "A").where("rank", "<", 5).find_nearest(**kw).get()) == ["near"]


async def test_the_other_invalid_argument_shape_checks_still_hold(db):
    col = _vectors(db)
    with pytest.raises(InvalidArgument):
        await col.where("acc", "in", [str(i) for i in range(31)]).get()
    with pytest.raises(InvalidArgument):
        await col.where("t", "array_contains", 1).where("u", "array_contains_any", [1]).get()


# --------------------------------------------------------------------------------------- opt-in index refusal

def _idx(*fields, group="g", scope="COLLECTION"):
    spelled = []
    for name, mode in fields:
        if mode in ("ASCENDING", "DESCENDING"):
            spelled.append({"fieldPath": name, "order": mode})
        elif mode == "CONTAINS":
            spelled.append({"fieldPath": name, "arrayConfig": "CONTAINS"})
        else:
            spelled.append({"fieldPath": name, "vectorConfig": {"dimension": 2, "flat": {}}})
    return {"collectionGroup": group, "queryScope": scope, "fields": spelled}


A, D = "ASCENDING", "DESCENDING"
DECLARED = [
    _idx(("acc", A), ("at", D)),
    _idx(("acc", A), ("kind", A), ("at", D)),
    _idx(("acc", A), ("n", A)),
    _idx(("acc", A), ("tags", "CONTAINS"), ("at", D)),
    _idx(("acc", A), ("cur", A), ("v", "VECTOR")),
    _idx(("user", A), ("seq", A), group="windows", scope="COLLECTION_GROUP"),
    _idx(("acc", A), ("other", A), group="elsewhere"),
    _idx(("v", "VECTOR"), group="vec"),
    _idx(("user", A), group="windows", scope="COLLECTION_GROUP"),
    _idx(("ts", D), group="windows", scope="COLLECTION_GROUP"),
]
OVERRIDES = [{"collectionGroup": "parts", "fieldPath": "user", "indexes": [{"queryScope": "COLLECTION_GROUP", "order": "ASCENDING"}]},
             {"collectionGroup": "parts", "fieldPath": "seq", "indexes": []}]


@pytest.fixture
def strict():
    db = EvalDb(indexes=DECLARED, groups={"eval_g": "g", "eval_vec": "vec"})
    return db, db.collection("eval_g")


@pytest.mark.parametrize("build", [
    lambda c: c.where("acc", "==", "a").order_by("at", direction=D),                         # declared
    lambda c: c.where("acc", "in", ["a", "b"]).where("kind", "==", "k").order_by("at", direction=D),
    lambda c: c.where("acc", "==", "a").where("n", ">", 1),                                  # equality + range, no ordering
    lambda c: c.where("acc", "==", "a").where("n", "!=", 1).order_by("n"),
    lambda c: c.where("acc", "==", "a").where("tags", "array_contains", "t").order_by("at", direction=D),
    lambda c: c.where("acc", "==", "a").where("cur", "==", True).find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=1),
    lambda c: c.where("acc", "==", "a").where("cur", ">=", 0).find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=1),
    # served by single-field indexes: no composite needed
    lambda c: c.where("acc", "==", "a").where("kind", "==", "k").where("zzz", "in", [1]),
    lambda c: c.where("n", ">", 1).where("n", "<", 5),
    lambda c: c.order_by("zzz"),
    lambda c: c.where("tags", "array_contains", "t"),
])
async def test_a_query_the_declared_indexes_serve_runs(strict, build):
    await build(strict[1]).get()


async def test_a_vector_query_with_or_without_pre_filter_needs_its_vector_index(strict):
    db, _ = strict
    kw = dict(vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=1)
    await db.collection("eval_vec").find_nearest(**kw).get()                                  # declared on `v`
    with pytest.raises(FailedPrecondition, match="fields \\['v \\(VECTOR\\)'\\]"):
        await db.collection("eval_g").find_nearest(**kw).get()                                # no vector index on `g`
    with pytest.raises(FailedPrecondition):
        await db.collection("eval_vec").find_nearest(**{**kw, "vector_field": "w"}).get()     # not on this field


@pytest.mark.parametrize("build", [
    lambda c: c.where("acc", "==", "a").order_by("zzz"),                                     # no such index
    lambda c: c.where("acc", "==", "a").order_by("at"),                                      # exact directions: (acc, at DESC)
    lambda c: c.where("zzz", "==", "a").where("n", ">", 1),                                  # equality + range, wrong field
    lambda c: c.where("acc", "==", "a").where("kind", ">", "k"),                             # range on a field no index has
    lambda c: c.where("acc", "in", ["a"]).where("zzz", "!=", 1),
    lambda c: c.order_by("acc").order_by("zzz"),                                             # two orderings
    lambda c: c.where("acc", "==", "a").where("tags", "array_contains", "t"),                # array + other filter, no order
    lambda c: c.where("zzz", "==", 1).where("tags", "array_contains", "t").order_by("at", direction=D),
    lambda c: c.where("zzz", "==", True).find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=1),        # vector, wrong pre-filter
    lambda c: c.where("acc", "==", "a").where("cur", "!=", 0).where("kind", "==", "k").find_nearest(
        vector_field="v", query_vector=[1.0, 0.0], distance_measure=COSINE, limit=1),
])
async def test_a_composite_query_no_declared_index_serves_is_refused_with_the_missing_index_named(strict, build):
    with pytest.raises(FailedPrecondition, match="requires an index: collection group 'g', scope COLLECTION"):
        await build(strict[1]).get()


async def test_the_refusal_names_the_fields_the_index_must_list(strict):
    with pytest.raises(FailedPrecondition) as err:
        await strict[1].where("acc", "==", "a").where("zzz", ">", 1).order_by("zzz", direction=D).get()
    assert "acc (EQ)" in str(err.value) and "zzz (DESCENDING)" in str(err.value)


async def test_directions_must_match_the_declared_index_exactly():
    db = EvalDb(indexes=[_idx(("acc", A), ("a", A), ("b", D))], groups={"eval_g": "g"})
    col = db.collection("eval_g")
    await col.where("acc", "==", 1).order_by("a").order_by("b", direction=D).get()
    for wrong in (lambda: col.order_by("a", direction=D).order_by("b"),         # P5 verifies: served in reverse?
                  lambda: col.order_by("a").order_by("b")):
        with pytest.raises(FailedPrecondition):
            await wrong().where("acc", "==", 1).get()


async def test_an_index_declared_for_another_group_or_scope_does_not_serve(strict):
    db, col = strict
    other = db.collection("eval_other")                          # unmapped: looked up under its own name
    with pytest.raises(FailedPrecondition):
        await other.where("acc", "==", "a").order_by("at", direction=D).get()
    await db.collection_group("windows").where("user", "==", "u").order_by("seq").get()
    with pytest.raises(FailedPrecondition):                      # `g`'s indexes are COLLECTION scope
        await db.collection_group("g").where("acc", "==", "a").order_by("at", direction=D).get()


async def test_a_collection_group_query_needs_a_collection_group_index_even_for_one_field(strict):
    db, _ = strict
    windows = db.collection_group("windows")
    await windows.get()                                          # a bare stream needs none
    await windows.select(["user"]).limit(3).get()
    await windows.where("user", "==", "u").get()                 # single-field COLLECTION_GROUP index on `user`
    await windows.order_by("ts", direction=D).get()
    with pytest.raises(FailedPrecondition, match="COLLECTION_GROUP"):
        await windows.where("zzz", "==", 1).get()                # the automatic index is COLLECTION scope only
    with pytest.raises(FailedPrecondition, match="COLLECTION_GROUP"):
        await db.collection_group("nowhere").order_by("seq").get()
    with pytest.raises(FailedPrecondition, match="COLLECTION_GROUP"):
        await windows.where("user", "array_contains", "u").get()   # an array filter needs an array index


async def test_a_collection_group_ordering_needs_its_exact_direction():
    db = EvalDb(indexes=[_idx(("ts", D), group="windows", scope="COLLECTION_GROUP"),
                         _idx(("user", D), group="windows", scope="COLLECTION_GROUP")])
    windows = db.collection_group("windows")
    await windows.order_by("ts", direction=D).get()
    await windows.where("user", "==", "u").get()                  # a filter is served by either direction
    with pytest.raises(FailedPrecondition, match="ASCENDING"):
        await windows.order_by("ts").get()                        # a DESC-only index does not serve an ascending order


async def test_a_non_empty_field_override_serves_a_collection_group_query_an_exemption_does_not():
    db = EvalDb(indexes=OVERRIDES)
    parts = db.collection_group("parts")
    await parts.where("user", "==", "u").get()
    with pytest.raises(FailedPrecondition):
        await parts.order_by("seq").get()                        # `indexes: []` declares no index (and is not applied)


async def test_the_refusal_is_off_unless_asked_for():
    plain = EvalDb()
    await plain.collection("anything").where("a", "==", 1).where("b", ">", 2).order_by("c").get()


async def test_indexes_can_be_read_from_the_real_index_file():
    from pathlib import Path
    path = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
    db = EvalDb(indexes=path, groups={"eval_facts": "development_domain_facts_v2"})
    facts = db.collection("eval_facts")
    await facts.where("lineage_id", "==", "l").order_by("created_at", direction=D).get()      # declared
    with pytest.raises(FailedPrecondition):
        await facts.where("lineage_id", "==", "l").order_by("no_such_field").get()
