"""FirestoreDriveIndexRepository's Search v2 methods on the wire (USER_DRIVE_INDEXING_RFC §25.2.2, §25.2.6, §25.4.5,
§25.5.5, §25.13.3): lean projections (no `word_keys`, text, items or vectors in a stream read), one disjunction list of
at most 30 values per query, write batches by bytes under Firestore's 10 MiB request limit, the hint and usage codecs,
server-side increments for usage, a transaction for the initial build, and the SDK's own encoding of what is sent."""
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from google.api_core import exceptions as gexc
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery
from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.async_query import AsyncQuery
from google.cloud.firestore_v1.types import Value
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import (
    CHUNK_LEAN_FIELDS, HINT_DISTANCE, HINT_FIELDS, HINT_LEAN_FIELDS, HINT_VECTOR, USAGE_EXPIRES_AT,
    FirestoreDriveIndexRepository, byte_batches, chunk_to_doc, hint_from_doc, hint_to_doc,
    initial_from_doc, initial_to_doc, stored_bytes, usage_from_doc,
)
from src.domain.drive_enrichment import DriveHintDoc, HintItem, InitialBuild, ModelUsageMonth
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveIndexStoreUnavailable, chunk_doc_id
from src.domain.drive_index_lean import RECORD_LEAN_FIELDS
from src.domain.drive_index_params import RECORD_WRITE_BATCH, WRITE_BATCH_BYTES
from src.domain.drive_usage import usage_expires_at

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.adapters.firestore_drive_eval_fakes import (
    CHUNKS, HINTS, RECS, USAGE, EvalDb, eval_transactions, make_eval_repo,
)
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)
FIRESTORE_REQUEST_LIMIT = 10 * 1024 * 1024
REALM, SUBJECT = "microsoft", "acct-1"   # the provider account an initial build is keyed by (MULTI_DRIVE_RFC §10)
HEAVY = ("text", "word_keys", "content_terms", "items", "profile", "readme_excerpt", "lead_text", "card_vector",
         "vector", HINT_VECTOR)


def hint(item="a", gen=1, index=0, *, keys=("w:roma",), kind="pdf", items=None):
    return DriveHintDoc(user_id="u", connection_id="u", item_id=item, generation=gen, index=index, kind=kind, chunk_text_hash="sha",
                        items=items if items is not None else (HintItem("Rome", "keyword", "en Roma", 12, 19),),
                        hint_keys=tuple(keys), passages_version=1)


def big_chunk(index, *, keys=900, text_chars=2_400):
    return DriveChunk(user_id="u", connection_id="u", item_id="big", generation=1, index=index, text="ё" * text_chars, char_start=0,
                      char_end=text_chars, kind="pdf", content_terms=tuple(f"{n:08d}" for n in range(64)),
                      word_keys=tuple(f"w:слово{n:05d}" for n in range(keys)), keys_version=1)


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def repo(db, mock_env_config):
    with eval_transactions():
        yield make_eval_repo(db, mock_env_config)


def selected(ops):
    return next(op[1] for op in ops if op[0] == "select")


def encoded_bytes(data):
    """The SDK's own encoding of a document's fields (protobuf bytes of each value plus its key)."""
    return sum(len(k.encode()) + Value.pb(v).ByteSize() for k, v in _helpers.encode_dict(data).items())


class TestLeanProjections:
    async def test_record_streams_read_only_the_lean_fields(self, repo, db):
        await repo.find_by_about_keys(U_SCOPE, ["e:rome"], 5)
        await repo.find_by_about_keys(U_SCOPE, ["e:rome"], 5, kind="pdf", date_from=T, date_to=T)
        await repo.find_expiring(U_SCOPE, ["lv:expir"], date(2026, 10, 13), 5)
        for ops in db.cols[RECS].executed:
            assert selected(ops) == (*RECORD_LEAN_FIELDS, "user_id")   # the owner check (I3)
            assert not set(selected(ops)) & set(HEAVY)

    async def test_get_records_lean_reads_only_the_lean_fields(self, repo, db):
        await repo.get_records_lean(U_SCOPE, ["a", "b"])
        ((ids, paths),) = db.get_all_calls
        assert ids == ["u:a", "u:b"] and tuple(paths) == (*RECORD_LEAN_FIELDS, "user_id")

    async def test_chunk_and_hint_streams_read_no_text_keys_items_or_vectors(self, repo, db):
        await repo.find_chunks_by_word_keys(U_SCOPE, ["w:rome"], 5, after_item_id="a")
        (ops,) = db.cols[CHUNKS].executed
        assert selected(ops) == CHUNK_LEAN_FIELDS == ("user_id", "item_id", "generation", "index", "kind")
        await repo.find_hints_by_keys(U_SCOPE, ["w:roma"], 5)
        await repo.hints_page(U_SCOPE, ("a", 1, 0), 5)
        await repo.find_nearest_hints(U_SCOPE, [1.0, 0.0], 5, kind="pdf")
        hint_ops = db.cols[HINTS].executed
        assert [selected(ops) for ops in hint_ops[:2]] == [HINT_LEAN_FIELDS] * 2
        assert set(selected(hint_ops[2])) == set(HINT_LEAN_FIELDS) | {HINT_DISTANCE}
        for ops in hint_ops:
            assert not set(selected(ops)) & set(HEAVY)
        assert "hint_keys" in HINT_LEAN_FIELDS and "items" not in HINT_LEAN_FIELDS

    async def test_key_reads_by_id_read_the_keys_only(self, repo, db):
        await repo.get_chunk_keys(U_SCOPE, [("a", 1, 0)])
        await repo.get_hint_keys(U_SCOPE, [("a", 1, 0)])
        assert [(ids, tuple(paths)) for ids, paths in db.get_all_calls] == [
            (["u:a:1:0"], ("user_id", "item_id", "generation", "index", "word_keys")),
            (["u:a:1:0"], ("user_id", "item_id", "generation", "index", "hint_keys"))]

    async def test_windows_read_whole_hint_documents_and_only_the_keys_pass_reads_vectors(self, repo, db):
        await repo.get_hints(U_SCOPE, "a", 1)
        await repo.get_hints_with_vectors(U_SCOPE, "a", 1)
        first, second = db.cols[HINTS].executed
        assert selected(first) == HINT_FIELDS and HINT_VECTOR not in selected(first)
        assert selected(second) == HINT_FIELDS + (HINT_VECTOR,)


class TestDisjunctions:
    async def test_one_key_list_of_at_most_thirty_and_kind_by_equality(self, repo, db):
        """Thirty distinct keys (a duplicate dropped) go out as one list, in order; more is refused before any query
        (review M1, arbiter: never a silent cut)."""
        for method in ("find_by_about_keys", "find_chunks_by_word_keys", "find_hints_by_keys"):
            with pytest.raises(ValueError, match="30"):
                await getattr(repo, method)(U_SCOPE, [f"e:k{i}" for i in range(40)], 5)
        assert all(not col.executed for col in db.cols.values())   # refused before any query
        keys = [f"e:k{i}" for i in range(30)] + ["e:k0"]
        await repo.find_by_about_keys(U_SCOPE, keys, 5, kind="pdf")
        await repo.find_chunks_by_word_keys(U_SCOPE, keys, 5)
        await repo.find_hints_by_keys(U_SCOPE, keys, 5)
        assert db.cols[RECS].executed and db.cols[CHUNKS].executed and db.cols[HINTS].executed
        for ops in db.cols[RECS].executed + db.cols[CHUNKS].executed + db.cols[HINTS].executed:
            lists = [op for op in ops if op[0] == "where" and op[2] in ("array_contains_any", "in")]
            assert len(lists) == 1 and lists[0][3] == [f"e:k{i}" for i in range(30)]
        assert ("where", "kind", "==", "pdf") in db.cols[RECS].executed[0]
        assert not any(op[0] == "where" and op[1] == "kind" for ops in db.cols[HINTS].executed for op in ops)

    async def test_job_state_counts_batch_the_kinds_by_thirty(self, repo, db):
        kinds = {f"kind{i:02d}" for i in range(65)}
        assert await repo.count_by_job_state(U_SCOPE, kinds) == {"profile": {}, "passages": {}}
        queries = db.cols[RECS].executed
        assert len(queries) == 3 * 2 * 6   # kind batches x jobs x states
        for ops in queries:
            (kind_in,) = [op for op in ops if op[0] == "where" and op[2] == "in"]
            assert kind_in[1] == "kind" and len(kind_in[3]) <= 30
            assert ops[-1][0] == "count"
        ready = [ops for ops in queries if ("where", "profile_state", "==", "ready") in ops]
        assert sorted(k for ops in ready for op in ops if op[0] == "where" and op[2] == "in" for k in op[3]) == sorted(
            kinds)   # every kind counted once per job and state

    async def test_no_keys_sends_no_query(self, repo, db):
        assert await repo.find_by_about_keys(U_SCOPE, [], 5) == []
        assert await repo.find_expiring(U_SCOPE, [], date(2026, 1, 1), 5) == []
        assert await repo.find_chunks_by_word_keys(U_SCOPE, [], 5) == []
        assert await repo.find_hints_by_keys(U_SCOPE, [], 5) == []
        assert all(not col.executed for col in db.cols.values())


class TestByteBatches:
    def test_the_estimate_is_not_below_the_sdk_encoding(self):
        data = {**chunk_to_doc(big_chunk(0)), "vector": Vector([0.123456789] * 768)}
        assert stored_bytes(data) >= encoded_bytes(data)
        hint_data = {**hint_to_doc(hint(items=tuple(HintItem(f"keyword {n}", "keyword", "a twelve word evidence "
                                                                "quote copied from the chunk text here", 0, 70)
                                                       for n in range(15)), keys=[f"w:k{n}" for n in range(60)])),
                     HINT_VECTOR: Vector([0.5] * 768)}
        assert stored_bytes(hint_data) >= encoded_bytes(hint_data)

    def test_counts_and_bytes_both_cut(self):
        assert [len(b) for b in byte_batches(list(range(RECORD_WRITE_BATCH + 1)), lambda _: 1)] == [RECORD_WRITE_BATCH,
                                                                                                  1]
        assert [len(b) for b in byte_batches(list(range(5)), lambda _: WRITE_BATCH_BYTES // 2)] == [1, 1, 1, 1, 1]
        assert [len(b) for b in byte_batches([0, 1], lambda _: WRITE_BATCH_BYTES * 2)] == [1, 1]   # alone, not lost
        assert byte_batches([], lambda _: 1) == []

    @pytest.mark.slow
    async def test_v2_chunks_are_written_under_the_request_limit(self, mock_env_config):
        db = ChunkDb()   # the recording client: batches are MagicMocks, nothing is copied
        chunks = [big_chunk(i) for i in range(RECORD_WRITE_BATCH)]
        await make_chunk_repo(db, mock_env_config).write_chunks(chunks, [[0.123456789] * 768] * len(chunks))
        one = encoded_bytes({**chunk_to_doc(chunks[0]), "vector": Vector([0.123456789] * 768)})   # all the same size
        sizes = [len(b.set.call_args_list) for b in db.batches]
        assert len(sizes) >= 2 and sum(sizes) == RECORD_WRITE_BATCH   # one batch of 400 would pass 10 MiB
        assert one * RECORD_WRITE_BATCH > FIRESTORE_REQUEST_LIMIT
        for b in db.batches:
            sent = [c.args[1] for c in b.set.call_args_list]
            assert len(sent) * one < FIRESTORE_REQUEST_LIMIT
            assert sum(stored_bytes(data) for data in sent) <= WRITE_BATCH_BYTES

    async def test_hint_documents_carry_their_user_and_are_batched(self, repo, db):
        docs = [hint(index=i) for i in range(RECORD_WRITE_BATCH + 1)]
        await repo.write_hints(docs, [[1.0, 0.0]] * len(docs))
        assert [len(c) for c in db.commits] == [RECORD_WRITE_BATCH, 1]
        stored = db.cols[HINTS].docs[chunk_doc_id(U_SCOPE, "a", 1, 7)]
        assert stored["user_id"] == "u" and stored[HINT_VECTOR] == Vector([1.0, 0.0])
        assert all(merge is False for c in db.commits for _, _, _, merge in c)   # whole documents, replaced

    async def test_vectors_must_match_the_documents(self, repo, db):
        with pytest.raises(ValueError):
            await repo.write_hints([hint(), hint(index=1)], [[1.0]])
        assert db.commits == []

    @pytest.mark.slow
    async def test_records_are_batched_by_bytes_too(self, mock_env_config):
        db = ChunkDb()
        records = [DriveIndexRecord(user_id="u", connection_id="u", item_id=f"r{i}", about_keys=tuple(f"e:k{n:04d}" * 4
                                                                                    for n in range(600)),
                                    readme_excerpt="x" * 7_500) for i in range(RECORD_WRITE_BATCH)]
        await make_chunk_repo(db, mock_env_config).upsert_records(records)
        assert len(db.batches) >= 2
        for b in db.batches:
            sent = [c.args[1] for c in b.set.call_args_list]
            assert all(c.kwargs["merge"] == list(c.args[1]) for c in b.set.call_args_list)
            assert sum(stored_bytes(data) for data in sent) <= WRITE_BATCH_BYTES


class TestCodecs:
    def test_hint_round_trip_and_sdk_encoding(self):
        doc = hint(items=(HintItem("Rome", "keyword", "en Roma", 12, 19), HintItem("Where?", "question", "x")),
                   keys=("w:rome", "w:рим"))
        data = hint_to_doc(doc)
        assert set(data) == set(HINT_FIELDS)
        assert data["items"][1] == {"text": "Where?", "type": "question", "evidence": "x", "evidence_start": -1,
                                    "evidence_end": -1}
        assert hint_from_doc({**data, HINT_VECTOR: Vector([1.0]), HINT_DISTANCE: 0.1}) == doc
        encoded = _helpers.encode_dict({**data, HINT_VECTOR: Vector([0.5])})
        assert encoded["items"].array_value.values[0].map_value.fields["evidence_start"].integer_value == 12

    def test_usage_reads_missing_fields_as_zero(self):
        assert usage_from_doc({}) == ModelUsageMonth()
        got = usage_from_doc({"triage_usd": 1, "calls": 3, "daily": {"2026-10-01": {"total_usd": 1}}})
        assert got == ModelUsageMonth(triage_usd=1.0, calls=3, daily={"2026-10-01": (1.0, 0.0)})

    def test_initial_build_round_trip_with_plain_datetimes(self):
        build = InitialBuild(40.0, 12.5, T, None)
        data = initial_to_doc("u", build)
        stored = {**data, "opened_at": DatetimeWithNanoseconds(2026, 10, 13, 9, 30, tzinfo=timezone.utc)}
        back = initial_from_doc(stored)
        assert back == build and type(back.opened_at) is datetime
        assert data["user_id"] == "u"


class TestUsageWrites:
    async def test_one_merge_with_increments_and_the_ttl(self, repo, db, monkeypatch):
        sent = []
        ref = db.collection(USAGE).document("u:2026-10")
        monkeypatch.setattr(type(ref), "set", lambda self, data, merge=False: _capture(sent, self, data, merge))
        await repo.add_model_usage("u", "2026-10", "2026-10-13", "passage", 0.5, 0.25, 2)
        ((doc_id, data, merge),) = sent
        assert doc_id == "u:2026-10" and merge is True
        assert data[USAGE_EXPIRES_AT] == usage_expires_at("2026-10") and data["user_id"] == "u"
        assert isinstance(data["passage_usd"], firestore.Increment) and data["passage_usd"].value == 0.5
        assert data["daily"]["2026-10-13"]["fallback_usd"].value == 0.25 and data["calls"].value == 2
        assert db.transactions == []
        _helpers.pbs_for_set_with_merge("projects/p/databases/d/documents/c/u:2026-10", data, merge=True)

    async def test_the_month_document_expires_and_the_initial_one_does_not(self, repo, db):
        await repo.add_model_usage("u", "2026-10", "2026-10-13", "reader", 0.5, 0.0, 1)
        await repo.spend_initial_build("u", REALM, SUBJECT, 1.0, T)
        docs = db.cols[USAGE].docs
        assert docs["u:2026-10"][USAGE_EXPIRES_AT] == datetime(2027, 11, 5, tzinfo=timezone.utc)
        assert USAGE_EXPIRES_AT not in docs["u:initial"] and docs["u:initial"]["user_id"] == "u"

    async def test_the_initial_build_is_spent_in_a_transaction(self, repo, db):
        await repo.spend_initial_build("u", REALM, SUBJECT, 1.0, T)
        assert [tx.writes for tx in db.transactions] == [[("set", "u:initial")]]
        await repo.close_initial_build("u", REALM, SUBJECT, T)
        await repo.close_initial_build("u", REALM, SUBJECT, T)            # already closed: nothing written
        await repo.spend_initial_build("u", REALM, SUBJECT, 1.0, T)        # closed: covers 0, nothing written
        await repo.get_initial_build("u", REALM, SUBJECT)
        assert [tx.writes for tx in db.transactions] == [[("set", "u:initial")], [("set", "u:initial")], [], []]


async def _capture(sent, ref, data, merge):
    sent.append((ref.id, data, merge))


def _real_repo(mock_env_config):
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    mock_env_config.drive_index_chunks_collection = "test_drive_index_chunks"
    mock_env_config.drive_index_hints_collection = "test_drive_index_hints"
    mock_env_config.drive_model_usage_collection = "test_drive_model_usage"
    return FirestoreDriveIndexRepository(client, mock_env_config)


class TestTheSdkBuildsTheQueries:
    async def test_the_about_cursor_is_the_cursor_records_date_and_id(self, monkeypatch, mock_env_config):
        sent = []

        async def capture(self, *args, **kwargs):
            sent.append(self._to_protobuf())
            return []

        snap = AsyncMock()
        snap.exists = True
        snap.to_dict = lambda: {"effective_date": T, "item_id": "c"}
        monkeypatch.setattr(AsyncQuery, "get", capture)
        monkeypatch.setattr(AsyncDocumentReference, "get", AsyncMock(return_value=snap))
        assert await _real_repo(mock_env_config).find_by_about_keys(U_SCOPE, ["e:rome"], 5, after="c") == []
        (query,) = sent
        assert [(o.field.field_path, o.direction.name) for o in query.order_by] == [
            ("effective_date", "DESCENDING"), ("item_id", "DESCENDING")]
        assert not query.start_at.before
        assert [v.string_value or v.timestamp_value for v in query.start_at.values][1] == "c"
        assert {f.field_path for f in query.select.fields} == set(RECORD_LEAN_FIELDS) | {"user_id"}

    async def test_a_missing_index_on_a_v2_read_is_a_store_outage(self, monkeypatch, mock_env_config):
        exc = gexc.FailedPrecondition("The query requires an index.")

        async def fail(self, *args, **kwargs):
            raise exc

        monkeypatch.setattr(AsyncQuery, "get", fail)
        monkeypatch.setattr(AsyncAggregationQuery, "get", fail)
        repo = _real_repo(mock_env_config)
        for call in (lambda: repo.find_by_about_keys(U_SCOPE, ["e:rome"], 5),
                     lambda: repo.find_hints_by_keys(U_SCOPE, ["w:roma"], 5),
                     lambda: repo.enrich_queue(U_SCOPE, 5),
                     lambda: repo.count_by_job_state(U_SCOPE, {"pdf"})):
            with pytest.raises(DriveIndexStoreUnavailable) as raised:
                await call()
            assert raised.value.__cause__ is exc
