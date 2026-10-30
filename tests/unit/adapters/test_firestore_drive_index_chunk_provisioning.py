"""Provisioning of the chunk queries (USER_DRIVE_INDEXING_RFC §12.5, §24.6, RFC Δ22): every chunk query that needs a
composite has one in config/firestore.indexes.json, under the live collection only; the readable-chunk sum has its
records index with `chunk_count` in it (an aggregation reads index entries only); `text` and `vector` are exempt from
single-field indexing, and no chunk query relies on a single-field index of an exempt field; `make deploy-indexes`
creates the composites and the exemptions while the vector entries are left to create_drive_vector_indexes.sh."""
import json
from pathlib import Path

import pytest

from tests.unit.adapters.firestore_drive_chunk_fakes import CHUNKS, ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_fakes import RECS, executed
from tests.unit.adapters.test_firestore_drive_index_provisioning import _deploy_commands
from tests.unit.services.drive_index_fakes import U_SCOPE

_ROOT = Path(__file__).resolve().parents[3]


def _raw():
    return json.loads((_ROOT / "config" / "firestore.indexes.json").read_text())


def _declared(collection):
    return [[(f["fieldPath"], f.get("order") or f.get("arrayConfig") or "VECTOR") for f in idx["fields"]]
            for idx in _raw() if idx["collectionGroup"] == collection and "fields" in idx]


def _exempt(collection):
    return {idx["fieldPath"] for idx in _raw()
            if idx["collectionGroup"] == collection and "fields" not in idx and idx.get("indexes") == []}


def _required(ops):
    """Equality / array filters (any order) first, then the range field, then the orderings or the vector field.
    None for equality-only queries without ordering (single-field indexes serve them)."""
    prefix = {(op[1], "CONTAINS" if op[2].startswith("array_contains") else "EQ")
              for op in ops if op[0] == "where" and op[2] in ("==", "array_contains", "array_contains_any")}
    ranged = [(op[1], "ASCENDING") for op in ops if op[0] == "where" and op[2] in (">", ">=", "<", "<=")]
    nearest = [op[1] for op in ops if op[0] == "find_nearest"]
    tail = ([(nearest[0]["vector_field"], "VECTOR")] if nearest
            else ranged + [(op[1], op[2]) for op in ops if op[0] == "order_by"])
    summed = [(op[1], "ASCENDING") for op in ops if op[0] == "sum"]
    tail = tail + summed if tail else tail
    return (prefix, tail) if tail else None


def _covered(required, index):
    prefix, tail = required
    head, rest = index[:len(index) - len(tail)], index[len(index) - len(tail):]
    return rest == tail and {(p, "CONTAINS" if m == "CONTAINS" else "EQ") for p, m in head} == prefix


@pytest.fixture
def db():
    return ChunkDb()


async def test_every_chunk_query_has_a_declared_index(db, mock_env_config):
    repo = make_chunk_repo(db, mock_env_config)
    await repo.find_nearest_chunks(U_SCOPE, [1.0], 5)
    await repo.find_nearest_chunks(U_SCOPE, [1.0], 5, kind="pdf")
    await repo.find_chunks_by_term(U_SCOPE, "1250", 5)
    await repo.find_chunks_by_term(U_SCOPE, "1250", 5, kind="pdf")
    await repo.chunks_page(U_SCOPE, None, 5)
    await repo.chunks_page(U_SCOPE, ("a", 1, 0), 5)
    await repo.delete_chunks_except(U_SCOPE, "a", 1)
    await repo.delete_user_chunks(U_SCOPE, 5)
    queries = executed(db, CHUNKS)
    assert len(queries) == 8
    declared = _declared("development_drive_index_chunks")
    needing = [ops for ops in queries if _required(ops) is not None]
    assert len(needing) == 6   # the two deletes read by equality only
    assert [ops for ops in needing if not any(_covered(_required(ops), i) for i in declared)] == []


async def test_the_readable_sum_has_its_records_index(db, mock_env_config):
    await make_chunk_repo(db, mock_env_config).readable_chunk_total(U_SCOPE)
    (ops,) = executed(db, RECS)
    required = _required(ops)
    assert required == ({("user_id", "EQ")}, [("indexed_generation", "ASCENDING"), ("chunk_count", "ASCENDING")])
    assert any(_covered(required, i) for i in _declared("development_drive_index_records"))


def test_chunk_indexes_are_declared_for_the_live_collection_only():
    # 4 composites (+ Search v2's word_keys stream, §25.2.5) + 3 vector entries (+ Δ246's user_id, item_id, vector)
    assert len(_declared("development_drive_index_chunks")) == 7
    assert [i for i in _declared("development_drive_index_chunks") if i[-1] == ("vector", "VECTOR")] == [
        [("user_id", "ASCENDING"), ("vector", "VECTOR")],
        [("user_id", "ASCENDING"), ("kind", "ASCENDING"), ("vector", "VECTOR")],
        [("user_id", "ASCENDING"), ("item_id", "ASCENDING"), ("vector", "VECTOR")]]
    assert _declared("drive_index_chunks") == []


def test_text_and_vector_are_exempt_from_single_field_indexes_on_the_live_collection_only():
    assert _exempt("development_drive_index_chunks") == {"text", "vector", "word_keys"}   # word_keys: §25.2.5
    assert not any("fields" not in idx and not idx["collectionGroup"].startswith("development_") for idx in _raw())


async def test_no_chunk_query_relies_on_an_exempt_field(db, mock_env_config):
    """An exemption must not take away an index a query uses. It removes only the automatic single-field indexes
    (Firestore: "A field exempted from automatic indexing can still be indexed as part of a manual index"), so a query
    that reads no composite never touches an exempt field, and a query on an exempt field is served by a declared
    composite that lists it. The only exempt field in a composite is Search v2's `word_keys`, as CONTAINS (§25.2.5);
    the vector field is served by its vector index."""
    repo = make_chunk_repo(db, mock_env_config)
    await repo.delete_chunks_except(U_SCOPE, "a", 1)
    await repo.delete_user_chunks(U_SCOPE, 5)
    await repo.find_chunks_by_term(U_SCOPE, "1250", 5)
    await repo.chunks_page(U_SCOPE, ("a", 1, 0), 5)
    await repo.find_chunks_by_word_keys(U_SCOPE, ["w:a"], 5)
    await repo.find_chunks_by_word_keys(U_SCOPE, ["w:a"], 5, after_item_id="a")
    exempt = _exempt("development_drive_index_chunks")
    declared = _declared("development_drive_index_chunks")
    queries = executed(db, CHUNKS)
    used = {op[1] for ops in queries for op in ops if op[0] in ("where", "order_by")}
    assert used & exempt == {"word_keys"}
    for ops in queries:
        fields = {op[1] for op in ops if op[0] in ("where", "order_by")}
        if _required(ops) is None:
            assert not fields & exempt, ops
        elif fields & exempt:
            assert any(_covered(_required(ops), i) and (fields & exempt) <= {p for p, _ in i} for i in declared), ops
    assert {(path, mode) for idx in declared for path, mode in idx
            if path in exempt and mode != "VECTOR"} == {("word_keys", "CONTAINS")}


def test_deploy_creates_the_chunk_composites_and_the_exemptions():
    chunk = [c for c in _deploy_commands() if "--collection-group=development_drive_index_chunks" in c]
    composites = [c for c in chunk if c[3] == "composite"]
    exemptions = [c for c in chunk if c[3] == "fields"]
    assert len(composites) == 4   # + Search v2's word_keys stream (§25.2.5)
    assert sum("--field-config=field-path=content_terms,array-config=contains" in c for c in composites) == 2
    assert sum("--field-config=field-path=word_keys,array-config=contains" in c for c in composites) == 1
    assert sorted(c[5] for c in exemptions) == ["text", "vector", "word_keys"]
    assert all("--disable-indexes" in c for c in exemptions)
    records = [c for c in _deploy_commands() if "--collection-group=development_drive_index_records" in c]
    assert sum("--field-config=field-path=chunk_count,order=ascending" in c for c in records) == 1


async def test_the_chunk_cap_and_unread_queries_have_their_records_indexes(db, mock_env_config):
    """Part B's release query and unread counts (§5.4, §10.2): an `in` or a null filter is an equality here."""
    repo = make_chunk_repo(db, mock_env_config)
    await repo.chunk_capped(U_SCOPE, 5)
    await repo.count_unread(U_SCOPE, {"pdf", "docx"})
    queries = executed(db, RECS)
    assert len(queries) == 7
    declared = _declared("development_drive_index_records")
    for ops in queries:
        prefix = {(op[1], "EQ") for op in ops if op[0] == "where" and op[2] in ("==", "IS_NULL", "in")}
        tail = [(op[1], op[2]) for op in ops if op[0] == "order_by"]
        if not tail:   # a count over equalities only: the composite holds exactly its fields, in any order
            assert any({(p, "EQ") for p, _ in i} == prefix for i in declared), ops
        else:
            assert any(_covered((prefix, tail), i) for i in declared), ops
