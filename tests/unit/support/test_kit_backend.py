"""The store kits' backend seam and the cloud gate's index model, offline (tests/support/kit_backend.py,
tests/support/cloud_indexes.py). The real backend's clients are replaced by in-memory stand-ins that record what the
seam sends; the run on the test database itself is `tests/cloud/test_kits_cloud.py`."""
from types import SimpleNamespace

import pytest

from tests.support.cloud_indexes import (
    admin_index, existing_signature, of_group, render_indexes, rendered_group, wanted_signature,
)
from tests.support.firestore_eval import EvalDb
from tests.support.kit_backend import INDEXES_FILE, RawDocs, RealKitBackend, live_group, real_name

SESSIONS_INDEX = {"collectionGroup": "development_sessions", "queryScope": "COLLECTION",
                  "fields": [{"fieldPath": "owner_id", "order": "ASCENDING"},
                             {"fieldPath": "last_activity", "order": "DESCENDING"},
                             {"fieldPath": "__name__", "order": "DESCENDING"}]}
SHAPES = [
    SESSIONS_INDEX,
    {"collectionGroup": "g", "queryScope": "COLLECTION_GROUP",
     "fields": [{"fieldPath": "tags", "arrayConfig": "CONTAINS"}, {"fieldPath": "at", "order": "ASCENDING"}]},
    {"collectionGroup": "g", "fields": [{"fieldPath": "account_id"},
                                        {"fieldPath": "vector", "vectorConfig": {"dimension": 768, "flat": {}}}]},
]


# --------------------------------------------------------------------------------------------------- the fixture

def test_the_unit_suite_kit_db_is_the_evaluating_fake(kit_db):
    assert kit_db is EvalDb


# --------------------------------------------------------------------------------------------------- names

def test_a_grouped_or_live_name_renders_to_the_test_prefix_and_any_other_is_namespaced():
    groups = {"eval_sessions": "development_sessions"}
    assert live_group("eval_sessions", groups) == "development_sessions"
    assert real_name("eval_sessions", groups) == "test_sessions"
    assert real_name("development_users", None) == "test_users"
    assert live_group("eval_other", groups) is None
    assert real_name("eval_other", groups) == "test_kits_eval_other"
    assert rendered_group("users") is None


# --------------------------------------------------------------------------------------------------- index model

def test_render_keeps_the_groups_composites_renamed_and_drops_overrides_and_other_groups():
    raw = [SESSIONS_INDEX,
           {"collectionGroup": "development_sessions", "fieldPath": "history", "indexes": []},
           {"collectionGroup": "development_users", "fields": [{"fieldPath": "a"}, {"fieldPath": "b"}]}]
    assert render_indexes("development_sessions", raw) == [dict(SESSIONS_INDEX, collectionGroup="test_sessions")]
    assert render_indexes("sessions", raw) == []


def test_render_reads_the_repository_index_file():
    rendered = render_indexes("development_sessions", INDEXES_FILE)
    assert rendered and {i["collectionGroup"] for i in rendered} == {"test_sessions"}


def test_the_wanted_signature_drops_name_and_reads_order_array_and_vector():
    assert wanted_signature(SESSIONS_INDEX) == ("COLLECTION", (("owner_id", "ASCENDING"), ("last_activity", "DESCENDING")))
    assert wanted_signature(SHAPES[1]) == ("COLLECTION_GROUP", (("tags", "CONTAINS"), ("at", "ASCENDING")))
    assert wanted_signature(SHAPES[2]) == ("COLLECTION", (("account_id", "ASCENDING"), ("vector", "VECTOR")))


@pytest.mark.parametrize("index", SHAPES, ids=["descending", "array-group", "vector"])
def test_an_admin_index_built_from_a_shape_has_that_shapes_signature(index):
    assert existing_signature(admin_index(index)) == wanted_signature(index)


def test_listed_indexes_are_kept_only_for_their_own_group():
    listed = [SimpleNamespace(name=f"projects/p/databases/d/collectionGroups/{g}/indexes/{n}")
              for g, n in (("test_sessions", "a"), ("test_sessions_old", "b"), ("test_parity_idx_knn", "c"))]
    assert [i.name[-1] for i in of_group(listed, "test_sessions")] == ["a"]


# --------------------------------------------------------------------------------------------------- raw view

class SyncRef:
    def __init__(self, store, doc_id):
        self.store, self.id = store, doc_id

    def set(self, data):
        self.store.writes.append(("set", self.id, dict(data)))
        self.store.docs[self.id] = dict(data)

    def delete(self):
        self.store.writes.append(("delete", self.id))
        self.store.docs.pop(self.id, None)


class SyncCollection:
    """A synchronous collection reference held in memory: what `RawDocs` reads and writes through."""

    def __init__(self, docs=None):
        self.docs, self.writes = dict(docs or {}), []

    def document(self, doc_id):
        return SyncRef(self, doc_id)

    def stream(self):
        return [SimpleNamespace(id=k, reference=SyncRef(self, k), to_dict=lambda v=v: dict(v)) for k, v in sorted(self.docs.items())]


def test_raw_docs_read_the_collection_and_write_documents_through():
    col = SyncCollection({"b1": {"status": "pending"}})
    raw = RawDocs(col)
    assert raw == {"b1": {"status": "pending"}}
    raw["b2"] = {"status": "failed"}
    del raw["b1"]
    assert raw.pop("nope", None) is None
    assert col.docs == {"b2": {"status": "failed"}}
    assert col.writes == [("set", "b2", {"status": "failed"}), ("delete", "b1")]


def test_a_raw_documents_top_level_field_change_rewrites_the_document():
    col = SyncCollection({"b1": {"status": "pending", "attempts": 1}})
    doc = RawDocs(col)["b1"]
    doc["routed_by"] = "overflow-v0"
    del doc["attempts"]
    assert col.docs == {"b1": {"status": "pending", "routed_by": "overflow-v0"}}
    with pytest.raises(NotImplementedError):
        doc.setdefault("x", 1)


# --------------------------------------------------------------------------------------------------- the backend

class Sync:
    def __init__(self):
        self.purged = []

    def collection(self, path):
        return SimpleNamespace(path=path)

    def recursive_delete(self, ref):
        self.purged.append(ref.path)


class Async:
    def __init__(self):
        self.groups = []

    def collection(self, path):
        return SimpleNamespace(path=path, document=lambda doc_id: f"{path}/{doc_id}")

    def collection_group(self, name):
        self.groups.append(name)
        return name

    def transaction(self):
        return "txn"


def test_the_backend_maps_names_makes_indexes_ready_once_and_purges_before_first_use_and_after():
    ensured, sync, client = [], Sync(), Async()
    backend = RealKitBackend(client, sync, lambda group, wanted: ensured.append((group, wanted)), [SESSIONS_INDEX])
    db = backend(indexes=INDEXES_FILE, groups={"eval_sessions": "development_sessions"})
    assert db.collection("eval_sessions").document("u1:C1") == "test_sessions/u1:C1"
    assert db.collection("eval_sessions/u1/versions").path == "test_sessions/u1/versions"
    assert db.collection("eval_notes").path == "test_kits_eval_notes"
    assert db.collection_group("versions") == "versions"
    assert db.transaction() == "txn"
    assert ensured == [("test_sessions", [dict(SESSIONS_INDEX, collectionGroup="test_sessions")])]
    assert sync.purged == ["test_sessions", "test_kits_eval_notes"]
    backend.purge_touched()
    assert sync.purged[2:] == ["test_kits_eval_notes", "test_sessions"]


# --------------------------------------------------------------------------------------------------- nothing is silently lost

def test_a_nested_raw_value_is_frozen_equal_to_the_plain_value_and_copies_thaw():
    import copy
    col = SyncCollection({"u1": {"config": {"tier": "eco"}, "history": [{"text": "hi"}]}})
    doc = RawDocs(col)["u1"]
    assert doc == {"config": {"tier": "eco"}, "history": [{"text": "hi"}]}
    for edit in (lambda: doc["config"].__setitem__("tier", "x"), lambda: doc["history"].append({}),
                 lambda: doc["history"][0].update(text="y"), lambda: doc["config"].pop("tier")):
        with pytest.raises(TypeError, match="frozen"):
            edit()
    plain = copy.deepcopy(doc["history"])
    plain[0]["text"] = "changed"
    assert type(plain) is list and type(plain[0]) is dict
    doc["config"] = {"tier": "performance"}
    with pytest.raises(TypeError):
        doc["config"]["tier"] = "eco"
    assert col.docs["u1"]["config"] == {"tier": "performance"} and type(col.writes[-1][2]["config"]) is dict


class Client(Async):
    def document(self, path):
        return f"doc:{path}"


def backend_and_db(groups=None):
    backend = RealKitBackend(Client(), Sync(), lambda *_: None, [SESSIONS_INDEX])
    return backend, backend(groups=groups or {"eval_sessions": "development_sessions"})


def test_a_public_attribute_set_on_the_database_or_a_collection_raises_except_yield_on_get():
    _, db = backend_and_db()
    col = db.collection("eval_sessions")
    col.yield_on_get = True
    for target in (col, db):
        with pytest.raises(AttributeError, match="h1_only"):
            target.fail = RuntimeError("down")


def test_document_paths_are_mapped_and_database_wide_calls_raise():
    _, db = backend_and_db()
    assert db.document("eval_sessions/u1:C1") == "doc:test_sessions/u1:C1"
    assert db.document("eval_notes", "n1") == "doc:test_kits_eval_notes/n1"
    with pytest.raises(NotImplementedError):
        db.collections()
    with pytest.raises(NotImplementedError):
        db.recursive_delete(db.collection("eval_sessions"))


def test_a_collection_group_is_mapped_when_grouped_or_already_used_and_left_alone_otherwise():
    backend, db = backend_and_db()
    assert db.collection_group("eval_sessions") == "test_sessions"
    assert db.collection_group("eval_notes") == "eval_notes"
    db.collection("eval_notes")
    assert db.collection_group("eval_notes") == "test_kits_eval_notes"
    assert backend.client.groups == ["test_sessions", "eval_notes", "test_kits_eval_notes"]


def test_an_index_set_the_real_database_cannot_honour_is_refused():
    backend, _ = backend_and_db()
    assert backend(indexes=None) and backend(indexes=INDEXES_FILE) and backend(indexes=str(INDEXES_FILE))
    for indexes in ([], [SESSIONS_INDEX], "elsewhere.json"):
        with pytest.raises(ValueError, match="h1_only"):
            backend(indexes=indexes)
