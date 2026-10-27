"""TaskSearchIndex kit (P11 · G3-1): the docstring of `src/ports/task_search_index.py` checked against
`FirestoreTaskSearchIndex` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on the raw
stored documents and on what the readers return.

Vectors are 2-dimensional on purpose (H1 does not check the declared 768). No test queries with `list_id`: no caller
passes it and no declared index serves it (the fake would refuse it as a missing index).

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_task_search_index import FirestoreTaskSearchIndex
from src.domain.task import TaskImportance, TaskSearchEntry, TaskStatus
from tests.support.firestore_eval import EvalDb, stored_vector
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_task_search_index"
VEC = [1.0, 0.0]
INDEXED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def _adapter(db: EvalDb) -> FirestoreTaskSearchIndex:
    return FirestoreTaskSearchIndex(db, SimpleNamespace(task_search_index_collection=COL))


class Store:
    """The real adapter over one EvalDb with the declared indexes, its collection mapped to its index group."""

    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={COL: "development_task_search_index"})
        self.index = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


@pytest.fixture
def store():
    return Store()


def entry(task_id="t1", user_id="u1", content=VEC, context=None, status=TaskStatus.NOT_STARTED, list_id="L1",
          title="Buy seeds for the community garden", **over) -> TaskSearchEntry:
    fields = dict(task_id=task_id, list_id=list_id, list_name="Garden", user_id=user_id, title=title, status=status,
                  short_id=f"s-{task_id}", content_vector=content, context_vector=context, indexed_at=INDEXED_AT)
    fields.update(over)
    return TaskSearchEntry(**fields)


def raw(store, task_id="t1", user_id="u1", content=VEC, **over) -> str:
    """Seed the raw document of `entry(task_id)` under `{user_id}_{task_id}` as the adapter writes it (vectors as
    `Vector`), with `over` on top; a value of `...` removes the field."""
    doc = {"task_id": task_id, "list_id": "L1", "list_name": "Garden", "user_id": user_id, "title": "Water the tomatoes",
           "status": "notStarted", "tags": [], "importance": "normal", "short_id": f"s-{task_id}",
           "content_vector": Vector(content), "context_vector": None, "indexed_at": INDEXED_AT}
    for field, value in over.items():
        if value is ...:
            doc.pop(field)
        else:
            doc[field] = value
    store.docs[f"{user_id}_{task_id}"] = doc
    return f"{user_id}_{task_id}"


async def ids(store, vectors=None, user_id="u1", **kw):
    return [e.task_id for e in await store.index.find_nearest(user_id, vectors or {"content_vector": VEC}, **kw)]


# --------------------------------------------------------------------------------------------------- upsert

async def test_upsert_stores_one_document_per_task_and_a_repeat_replaces_it(store):
    await store.index.upsert(entry(context=[0.0, 1.0], importance=TaskImportance.HIGH, tags=["seeds"]))
    await store.index.upsert(entry(title="Buy seeds and compost", context=[0.0, 1.0], importance=TaskImportance.HIGH))
    assert list(store.docs) == ["u1_t1"]
    doc = store.docs["u1_t1"]
    assert (doc["title"], doc["status"], doc["importance"], doc["tags"]) == ("Buy seeds and compost", "notStarted", "high", [])
    assert (doc["task_id"], doc["list_id"], doc["list_name"], doc["user_id"], doc["short_id"]) == ("t1", "L1", "Garden", "u1", "s-t1")
    assert isinstance(doc["content_vector"], Vector) and stored_vector(doc["content_vector"]) == VEC
    assert stored_vector(doc["context_vector"]) == [0.0, 1.0]
    assert doc["indexed_at"] == INDEXED_AT


async def test_upsert_without_vectors_stores_nulls_and_the_entry_is_never_found_by_that_field(store):
    await store.index.upsert(entry(content=None, context=[1.0, 0.0]))
    assert store.docs["u1_t1"]["content_vector"] is None
    assert await ids(store, {"content_vector": VEC}) == []
    assert await ids(store, {"context_vector": VEC}) == ["t1"]


# --------------------------------------------------------------------------------------------------- delete

async def test_delete_removes_only_that_task_and_a_missing_one_is_a_no_op(store):
    for user, task in (("u1", "t1"), ("u1", "t2"), ("u2", "t1")):
        raw(store, task, user)
    await store.index.delete("u1", "t1")
    await store.index.delete("u1", "t1")
    await store.index.delete("u1", "never-indexed")
    assert sorted(store.docs) == ["u1_t2", "u2_t1"]


async def test_delete_all_for_user_leaves_other_users_and_a_repeat_is_a_no_op(store):
    for task in ("t1", "t2", "t3"):
        raw(store, task, "u1")
    raw(store, "t1", "u2")
    await store.index.delete_all_for_user("u1")
    assert sorted(store.docs) == ["u2_t1"]
    await store.index.delete_all_for_user("u1")
    await store.index.delete_all_for_user("u-nobody")
    assert sorted(store.docs) == ["u2_t1"]


async def test_delete_all_for_user_goes_in_batches_of_500(store):
    for i in range(501):
        raw(store, f"t{i:03d}", "u1")
    raw(store, "t1", "u2")
    await store.index.delete_all_for_user("u1")
    assert sorted(store.docs) == ["u2_t1"]
    assert [len(c) for c in store.db.commits] == [500, 1]


async def test_delete_by_list_removes_one_list_of_one_user(store):
    raw(store, "t1", "u1", list_id="L1")
    raw(store, "t2", "u1", list_id="L2")
    raw(store, "t1", "u2", list_id="L1")
    await store.index.delete_by_list("u1", "L1")
    assert sorted(store.docs) == ["u1_t2", "u2_t1"]
    await store.index.delete_by_list("u1", "L1")
    assert sorted(store.docs) == ["u1_t2", "u2_t1"]


# ------------------------------------------------------------------------------------------- find_nearest

async def test_find_nearest_scopes_to_the_user_and_hides_completed(store):
    await store.index.upsert(entry("t1"))
    await store.index.upsert(entry("t2", status=TaskStatus.COMPLETED))
    await store.index.upsert(entry("t3", user_id="u2"))
    raw(store, "t4", "u1", status=...)
    assert await ids(store) == ["t1"]
    assert await ids(store, user_id="u2") == ["t3"]
    assert await ids(store, user_id="u-nobody") == []


async def test_find_nearest_show_completed_includes_completed_and_skips_an_entry_with_no_status(store):
    await store.index.upsert(entry("t1"))
    await store.index.upsert(entry("t2", status=TaskStatus.COMPLETED, content=[0.9, 0.1]))
    raw(store, "t4", "u1", status=...)      # found by the vector query, does not parse: skipped
    got = await store.index.find_nearest("u1", {"content_vector": VEC}, show_completed=True)
    assert [(e.task_id, e.status) for e in got] == [("t1", TaskStatus.NOT_STARTED), ("t2", TaskStatus.COMPLETED)]


async def test_find_nearest_returns_the_best_match_first_and_at_most_limit(store):
    for task, vector in (("t1", [1.0, 0.0]), ("t2", [0.9, 0.1]), ("t3", [0.8, 0.6]), ("t4", [0.7, 0.7])):
        await store.index.upsert(entry(task, content=vector))
    assert await ids(store, limit=2) == ["t1", "t2"]
    assert await ids(store, limit=10) == ["t1", "t2", "t3", "t4"]
    assert await ids(store, {"content_vector": [0.7, 0.7]}, limit=1) == ["t4"]


async def test_find_nearest_fuses_the_fields_so_a_match_on_both_outranks_the_best_match_on_one(store):
    await store.index.upsert(entry("only-content", content=[1.0, 0.0], context=None))
    await store.index.upsert(entry("both", content=[0.9, 0.1], context=[1.0, 0.0]))
    await store.index.upsert(entry("far", content=[0.8, 0.6], context=None))
    vectors = {"content_vector": VEC, "context_vector": VEC}
    assert await ids(store, vectors) == ["both", "only-content", "far"]
    assert await ids(store, vectors, limit=2) == ["both", "only-content"]


async def test_find_nearest_skips_a_none_vector_and_a_field_that_matches_nothing(store):
    await store.index.upsert(entry("t1", content=VEC))
    assert await ids(store, {"content_vector": VEC, "context_vector": None}) == ["t1"]
    assert await ids(store, {"content_vector": VEC, "context_vector": VEC}) == ["t1"]


async def test_find_nearest_drops_entries_beyond_the_similarity_floor(store):
    await store.index.upsert(entry("near", content=[0.7, 0.7]))
    await store.index.upsert(entry("orthogonal", content=[0.0, 1.0]))
    await store.index.upsert(entry("opposite", content=[-1.0, 0.0]))
    await store.index.upsert(entry("past-the-floor", content=[0.5, 0.866]))
    assert await ids(store, limit=10) == ["near"]


async def test_find_nearest_without_vectors_returns_empty_and_asks_the_backend_nothing(store):
    await store.index.upsert(entry("t1"))
    assert await store.index.find_nearest("u1", {}) == []
    assert await store.index.find_nearest("u1", {"content_vector": None}) == []
    assert store.db.collection(COL).executed == []


async def test_find_nearest_skips_entries_that_do_not_parse_and_never_finds_a_non_vector_value(store):
    raw(store, "ok", "u1")
    raw(store, "archived", "u1", status="archived")
    raw(store, "no-date", "u1", indexed_at=...)
    raw(store, "bad-importance", "u1", importance="urgent")
    raw(store, "iso-date", "u1", indexed_at="2026-10-01T09:00:00")        # parses (pydantic reads the string)
    assert sorted(await ids(store, limit=10)) == ["iso-date", "ok"]
    # a valid document whose vector is a plain list, not a Vector value, is never found by a vector query
    raw(store, "list-vector", "u1", content_vector=list(VEC))
    assert sorted(await ids(store, limit=10)) == ["iso-date", "ok"]


async def test_find_nearest_backend_up_answers(store):
    """The passing control of the P11-G3-1 pin: with the backend up the task is found."""
    await store.index.upsert(entry("t1"))
    assert await ids(store) == ["t1"]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G3-1: every vector query failed, find_nearest answers [] (TasksAgent: 'no tasks')")
async def test_find_nearest_raises_when_every_query_fails(store):
    """When every field query fails (an outage, a missing index) the caller must get the error, not "no matching
    task". Flipping fix: after the gather in `find_nearest`, `if all(isinstance(r, BaseException) for r in results_lists):
    raise results_lists[0]`."""
    await store.index.upsert(entry("t1"))
    require(await ids(store) == ["t1"], "the task is found while the backend is up")
    store.db.collection(COL).fail = ServiceUnavailable("down")
    raised, got = None, None
    try:
        got = await store.index.find_nearest("u1", {"content_vector": VEC})
    except Exception as exc:
        raised = exc
    assert raised is not None, f"every query failed and find_nearest answered {got!r}"


async def test_find_nearest_answers_from_the_fields_that_work_when_one_field_query_fails(store):
    await store.index.upsert(entry("t1", content=VEC, context=VEC))
    check_index = store.db._check_index

    def refuse_the_context_index(name, is_group, ops):
        if any(op[0] == "find_nearest" and op[1]["vector_field"] == "context_vector" for op in ops):
            raise FailedPrecondition("the index is not ready")
        return check_index(name, is_group, ops)
    store.db._check_index = refuse_the_context_index
    assert await ids(store, {"content_vector": VEC, "context_vector": VEC}) == ["t1"]


# ----------------------------------------------------------------------------------------- get_by_short_id

async def test_get_by_short_id_is_scoped_to_the_user_and_an_unparseable_entry_reads_none(store):
    await store.index.upsert(entry("t1", short_id="a1b2c3d4"))
    got = await store.index.get_by_short_id("u1", "a1b2c3d4")
    assert (got.task_id, got.user_id, got.title) == ("t1", "u1", "Buy seeds for the community garden")
    assert await store.index.get_by_short_id("u2", "a1b2c3d4") is None
    assert await store.index.get_by_short_id("u1", "ffffffff") is None
    raw(store, "t2", "u1", short_id="deadbeef", status="archived")
    assert await store.index.get_by_short_id("u1", "deadbeef") is None


async def test_get_by_short_id_propagates_backend_errors(store):
    await store.index.upsert(entry("t1", short_id="a1b2c3d4"))
    store.db.collection(COL).fail = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        await store.index.get_by_short_id("u1", "a1b2c3d4")
