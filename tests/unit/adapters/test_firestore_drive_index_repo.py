"""FirestoreDriveIndexRepository wire tests at the Firestore client boundary (USER_DRIVE_INDEXING_RFC §4, §7.1,
§7.5, §10.1, §12.5): the exact queries (filters, order, limit, projection), the writes, and the lease transaction.
Reference semantics: tests/unit/adapters/test_in_memory_drive_index_repo.py."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from google.api_core.exceptions import NotFound
from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_drive_index_repo import (
    _DOC_OVERHEAD_BYTES, CARD_DISTANCE, RECORD_FIELDS, STATE_FIELDS, _write_bytes, state_to_doc,
)
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Priority
from src.domain.drive_index_params import RECORD_WRITE_BATCH, WRITE_BATCH_BYTES

from tests.unit.adapters.firestore_drive_fakes import (
    NOW, PROJECTION, RECS, STATES, TS_MIN, Db, executed, make_repo, rec_doc, snap,
)
from tests.unit.services.drive_index_fakes import U_SCOPE


@pytest.fixture
def db():
    return Db()


@pytest.fixture
def repo(db, mock_env_config):
    return make_repo(db, mock_env_config)


@pytest.fixture(autouse=True)
def passthrough_transactions():
    with patch("src.adapters.firestore_drive_index_repo.firestore.async_transactional", side_effect=lambda fn: fn):
        yield


# -- sync state ---------------------------------------------------------------------------------

class TestSyncState:
    async def test_get_missing_is_none(self, repo):
        assert await repo.get_sync_state(U_SCOPE) is None

    async def test_get_reads_flags_and_fields(self, repo, db):
        doc = {**state_to_doc(DriveSyncState(user_id="u", connection_id="u", epoch=3)), "stale_alerted": True}
        db.cols[STATES].document("u").get = AsyncMock(return_value=snap(doc, "u"))
        st = await repo.get_sync_state(U_SCOPE)
        assert st.epoch == 3 and st.stale_alerted is True and st.reconnect_notified is False

    async def test_save_merges_every_field_but_the_flags(self, repo, db):
        await repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", epoch=3, stale_alerted=True))
        doc = db.cols[STATES].docs["u"]
        assert doc.get.call_args.kwargs["transaction"] is db.tx      # the fenced write (§7.1) reads in the transaction
        ref, data = db.tx.set.call_args.args
        merge = db.tx.set.call_args.kwargs["merge"]
        assert ref is doc and merge == list(STATE_FIELDS) and set(data) == set(STATE_FIELDS)
        assert "stale_alerted" not in data and "reconnect_notified" not in data
        assert data["epoch"] == 3

    async def test_update_flags_writes_only_the_given_flags(self, repo, db):
        await repo.update_sync_flags(U_SCOPE, reconnect_notified=True)
        db.cols[STATES].docs["u"].update.assert_awaited_once_with({"reconnect_notified": True})

    async def test_update_flags_without_values_writes_nothing(self, repo, db):
        await repo.update_sync_flags(U_SCOPE)
        assert "u" not in db.cols[STATES].docs

    async def test_update_flags_on_missing_state_is_a_noop(self, repo, db):
        doc = db.cols[STATES].document("u")
        doc.update = AsyncMock(side_effect=NotFound("no doc"))
        await repo.update_sync_flags(U_SCOPE, stale_alerted=True)   # no raise
        doc.set.assert_not_awaited()

    async def test_delete(self, repo, db):
        doc = db.cols[STATES].document("u")
        doc.get = AsyncMock(return_value=snap(state_to_doc(DriveSyncState(user_id="u", connection_id="u", epoch=2)), "u"))
        await repo.delete_sync_state(U_SCOPE)
        db.tx.delete.assert_called_once_with(doc)                    # in the fencing transaction (§7.1)

    async def test_list_user_ids_reads_names_only(self, repo, db):
        db.cols[STATES].respond = lambda ops: [snap({}, "u2"), snap({}, "u1")]
        assert await repo.list_sync_user_ids() == ["u1", "u2"]
        assert db.cols[STATES].executed == [[("select", ("__name__",))]]


class TestLease:
    def _stored(self, db, **kw):
        doc = state_to_doc(DriveSyncState(user_id="u", connection_id="u", **kw))
        db.cols[STATES].document("u").get = AsyncMock(return_value=snap(doc, "u"))

    async def test_missing_state_without_create_writes_nothing(self, repo, db):
        assert await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + timedelta(minutes=31), create=False) is None
        db.tx.set.assert_not_called()
        db.tx.update.assert_not_called()

    async def test_missing_state_with_create_writes_a_fresh_state_without_flags(self, repo, db):
        until = NOW + timedelta(minutes=31)
        st = await repo.acquire_lease(U_SCOPE, "a", NOW, until, create=True)
        assert st == DriveSyncState(user_id="u", connection_id="u", lease_owner="a", lease_until=until)
        ref, data = db.tx.set.call_args.args
        assert ref is db.cols[STATES].docs["u"]
        assert data["lease_owner"] == "a" and data["lease_until"] == until
        assert "stale_alerted" not in data and "reconnect_notified" not in data
        assert db.cols[STATES].docs["u"].get.call_args.kwargs["transaction"] is db.tx

    async def test_live_lease_of_another_owner_is_busy(self, repo, db):
        self._stored(db, lease_owner="b", lease_until=NOW + timedelta(seconds=1))
        assert await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + timedelta(minutes=31), create=True) is None
        db.tx.update.assert_not_called()
        db.tx.set.assert_not_called()

    async def test_expired_lease_is_taken_with_a_field_update(self, repo, db):
        self._stored(db, epoch=4, lease_owner="b", lease_until=NOW)
        until = NOW + timedelta(minutes=31)
        st = await repo.acquire_lease(U_SCOPE, "a", NOW, until, create=True)
        assert st.epoch == 4 and st.lease_owner == "a"
        db.tx.update.assert_called_once_with(db.cols[STATES].docs["u"], {"lease_owner": "a", "lease_until": until})
        db.tx.set.assert_not_called()

    async def test_own_live_lease_is_renewed(self, repo, db):
        self._stored(db, lease_owner="a", lease_until=NOW + timedelta(minutes=5))
        assert (await repo.acquire_lease(U_SCOPE, "a", NOW, NOW + timedelta(minutes=31), create=True)).lease_owner == "a"

    async def test_release_by_owner_clears_the_lease(self, repo, db):
        self._stored(db, lease_owner="a", lease_until=NOW)
        await repo.release_lease(U_SCOPE, "a")
        db.tx.update.assert_called_once_with(db.cols[STATES].docs["u"], {"lease_owner": None, "lease_until": None})

    async def test_release_by_another_owner_or_missing_state_is_a_noop(self, repo, db):
        await repo.release_lease(U_SCOPE, "a")              # missing
        self._stored(db, lease_owner="b", lease_until=NOW)
        await repo.release_lease(U_SCOPE, "a")              # not the owner
        db.tx.update.assert_not_called()


# -- record writes --------------------------------------------------------------------------------

class TestRecordWrites:
    async def test_upsert_merges_every_record_field_and_keeps_the_vector(self, repo, db):
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="a", has_card=True)])
        ref, data = db.batches[0].set.call_args.args
        assert ref is db.cols[RECS].docs["u:a"]
        assert db.batches[0].set.call_args.kwargs["merge"] == list(RECORD_FIELDS)
        assert "card_vector" not in data and "has_card_vector" not in data
        db.batches[0].commit.assert_awaited_once()

    async def test_given_vector_is_stored_with_its_mirror(self, repo, db):
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="a", has_card=True)], {"a": [1, 0.5]})
        _, data = db.batches[0].set.call_args.args
        assert data["card_vector"] == Vector([1.0, 0.5]) and data["has_card_vector"] is True
        assert set(db.batches[0].set.call_args.kwargs["merge"]) == set(RECORD_FIELDS) | {"card_vector",
                                                                                         "has_card_vector"}

    async def test_cardless_record_deletes_its_vector(self, repo, db):
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="a", has_card=False)])
        _, data = db.batches[0].set.call_args.args
        assert data["card_vector"] is firestore.DELETE_FIELD and data["has_card_vector"] is False
        assert "card_vector" in db.batches[0].set.call_args.kwargs["merge"]

    @pytest.mark.slow
    async def test_writes_are_batched_at_record_write_batch(self, repo, db):
        """Both caps bind record writes (RFC Δ245): RECORD_WRITE_BATCH, and WRITE_BATCH_BYTES over request plus
        index-entry bytes. A default record's ~140 indexed fields put the byte cap below 400 records a commit."""
        records = [DriveIndexRecord(user_id="u", connection_id="u", item_id=f"i{n}") for n in range(401)]
        await repo.upsert_records(records)
        written = [c.args for b in db.batches for c in b.set.call_args_list]
        assert [ref.doc_id for ref, _ in written] == [f"u:i{n}" for n in range(401)]   # all, once, in order
        sizes = [[_write_bytes(c.args) + _DOC_OVERHEAD_BYTES for c in b.set.call_args_list] for b in db.batches]
        assert len(sizes) >= 2
        for n, batch in enumerate(sizes):
            assert len(batch) <= RECORD_WRITE_BATCH and sum(batch) <= WRITE_BATCH_BYTES
            if n + 1 < len(sizes):   # closed only because the next write would not fit
                assert sum(batch) + sizes[n + 1][0] > WRITE_BATCH_BYTES

    @pytest.mark.slow
    async def test_record_count_cap_holds_when_bytes_do_not_bind(self, repo, db):
        with patch("src.adapters.firestore_drive_index_repo.WRITE_BATCH_BYTES", 10 ** 12):
            await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id=f"i{n}") for n in range(401)])
        assert [b.set.call_count for b in db.batches] == [RECORD_WRITE_BATCH, 1]

    async def test_due_without_priority_is_refused_before_any_write(self, repo, db):
        ok = DriveIndexRecord(user_id="u", connection_id="u", item_id="a", due_at=NOW, priority=0)
        stuck = DriveIndexRecord(user_id="u", connection_id="u", item_id="b", due_at=NOW)
        with pytest.raises(ValueError, match="due without a priority"):
            await repo.upsert_records([ok, stuck])
        assert db.batches == []

    async def test_priority_without_due_is_allowed(self, repo, db):
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="a", priority=0)])
        assert db.batches[0].set.call_count == 1

    async def test_delete_records_by_key_deduplicated(self, repo, db):
        await repo.delete_records("u", ["a", "b", "a"])
        assert [c.args[0].doc_id for c in db.batches[0].delete.call_args_list] == ["u:a", "u:b"]
        db.batches[0].commit.assert_awaited_once()

    async def test_delete_user_records_is_bounded_and_counts(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap({}, "u:a"), snap({}, "u:b")]
        assert await repo.delete_user_records("u", 2) == 2
        assert executed(db) == [[("where", "user_id", "==", "u"), ("select", ("__name__",)), ("limit", 2)]]
        assert [c.args[0].doc_id for c in db.batches[0].delete.call_args_list] == ["u:a", "u:b"]

    async def test_delete_user_records_when_empty(self, repo, db):
        assert await repo.delete_user_records("u", 400) == 0
        assert db.batches == []


# -- record reads ---------------------------------------------------------------------------------

class TestRecordReads:
    async def test_get_records_batched_projected_and_deduplicated(self, repo, db):
        db.stored = {"u:a": rec_doc("a", name="x"), "u:b": rec_doc("b")}
        out = await repo.get_records(U_SCOPE, ["a", "zz", "a", "b"])
        assert set(out) == {"a", "b"} and out["a"].name == "x"
        assert db.get_all_calls == [(["u:a", "u:zz", "u:b"], list(RECORD_FIELDS))]

    @pytest.mark.slow
    async def test_get_records_chunks_large_reads(self, repo, db):
        await repo.get_records(U_SCOPE, [f"i{n}" for n in range(401)])
        assert [len(ids) for ids, _ in db.get_all_calls] == [400, 1]

    async def test_due_records_one_query_per_class_in_order(self, repo, db):
        by_class = {int(Priority.EDIT): [rec_doc("b", due_at=NOW, priority=0)],
                    int(Priority.FIRST_SEEN): [rec_doc("a", due_at=NOW, priority=2),
                                               rec_doc("c", due_at=NOW, priority=2)]}
        db.cols[RECS].respond = lambda ops: [snap(d) for d in by_class.get(ops[1][3], [])]
        out = await repo.due_records(U_SCOPE, NOW, 10)
        assert [r.item_id for r in out] == ["b", "a", "c"]
        queries = executed(db)
        assert [q[1][3] for q in queries] == [0, 1, 2, 3, 4]
        assert queries[0] == [("where", "user_id", "==", "u"), ("where", "priority", "==", 0),
                              ("where", "due_at", "<=", NOW), ("order_by", "due_at", "ASCENDING"),
                              ("order_by", "item_id", "ASCENDING"), ("limit", 10), PROJECTION]
        assert queries[2][5] == ("limit", 9)

    async def test_due_records_stops_once_the_page_is_full(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap(rec_doc("b", due_at=NOW, priority=0))]
        assert [r.item_id for r in await repo.due_records(U_SCOPE, NOW, 1)] == ["b"]
        assert len(executed(db)) == 1

    async def test_oldest_due_at_excludes_undated_and_uses_the_due_index(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap(rec_doc("a", due_at=NOW, priority=2))]
        assert await repo.oldest_due_at(U_SCOPE, 2) == NOW
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "priority", "==", 2),
                                 ("where", "due_at", ">=", TS_MIN), ("order_by", "due_at", "ASCENDING"),
                                 ("order_by", "item_id", "ASCENDING"), ("limit", 1), PROJECTION]]

    async def test_oldest_due_at_none(self, repo):
        assert await repo.oldest_due_at(U_SCOPE, 0) is None

    async def test_children_paged_by_item_id(self, repo, db):
        await repo.children(U_SCOPE, "p", None, 5)
        await repo.children(U_SCOPE, "p", "a", 2)
        assert executed(db) == [
            [("where", "user_id", "==", "u"), ("where", "parent_id", "==", "p"),
             ("order_by", "item_id", "ASCENDING"), ("limit", 5), PROJECTION],
            [("where", "user_id", "==", "u"), ("where", "parent_id", "==", "p"), ("where", "item_id", ">", "a"),
             ("order_by", "item_id", "ASCENDING"), ("limit", 2), PROJECTION]]

    async def test_records_page(self, repo, db):
        await repo.records_page(U_SCOPE, "m", 100)
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "item_id", ">", "m"),
                                 ("order_by", "item_id", "ASCENDING"), ("limit", 100), PROJECTION]]

    async def test_records_seen_before(self, repo, db):
        await repo.records_seen_before(U_SCOPE, 3, 400)
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "seen_epoch", "<", 3),
                                 ("order_by", "seen_epoch", "ASCENDING"), ("order_by", "item_id", "ASCENDING"),
                                 ("limit", 400), PROJECTION]]

    async def test_card_capped_best_rank_then_newest(self, repo, db):
        await repo.card_capped("u", 7)
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "card_capped", "==", True),
                                 ("order_by", "card_rank", "ASCENDING"),
                                 ("order_by", "modified_at", "DESCENDING"), ("limit", 7), PROJECTION]]


class TestCountsAndEviction:
    @staticmethod
    def _counts(table):
        def respond(ops):
            key = next((op[3] for op in ops if op[0] == "where" and op[1] in ("state", "has_card_vector")), None)
            return [[SimpleNamespace(alias="n", value=table.get(key, 0))]]
        return respond

    async def test_count_carded_is_an_aggregation_on_the_vector_mirror(self, repo, db):
        db.cols[RECS].respond = self._counts({True: 4})
        assert await repo.count_carded(U_SCOPE) == 4
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "has_card_vector", "==", True),
                                 ("count", "n")]]

    async def test_count_by_state_omits_empty_states_and_adds_carded(self, repo, db):
        db.cols[RECS].respond = self._counts({"metadata_only": 3, "indexed": 1, True: 2})
        assert await repo.count_by_state(U_SCOPE) == {"metadata_only": 3, "indexed": 1, "carded": 2}
        assert len(executed(db)) == 7 and all(q[-1] == ("count", "n") for q in executed(db))

    async def test_lowest_ranked_none_when_nothing_carded(self, repo, db):
        assert await repo.lowest_ranked_carded("u") is None
        assert len(executed(db)) == 1

    async def test_lowest_ranked_takes_the_oldest_dated_record_of_the_top_rank(self, repo, db):
        def respond(ops):
            if ("order_by", "card_rank", "DESCENDING") in ops:
                return [snap(rec_doc("x", card_rank=2))]
            return [snap(rec_doc("old", card_rank=2, modified_at=NOW))]
        db.cols[RECS].respond = respond
        assert (await repo.lowest_ranked_carded("u")).item_id == "old"
        carded = [("where", "user_id", "==", "u"), ("where", "has_card_vector", "==", True)]
        assert executed(db) == [
            carded + [("order_by", "card_rank", "DESCENDING"), ("limit", 1), PROJECTION],
            carded + [("where", "card_rank", "==", 2), ("where", "modified_at", ">=", TS_MIN),
                      ("order_by", "modified_at", "ASCENDING"), ("order_by", "item_id", "ASCENDING"), ("limit", 1),
                      PROJECTION]]

    async def test_lowest_ranked_falls_back_to_undated_records(self, repo, db):
        def respond(ops):
            if ("order_by", "card_rank", "DESCENDING") in ops:
                return [snap(rec_doc("x", card_rank=3))]
            if ("where", "modified_at", "IS_NULL", None) in ops:
                return [snap(rec_doc("undated", card_rank=3))]
            return []
        db.cols[RECS].respond = respond
        assert (await repo.lowest_ranked_carded("u")).item_id == "undated"
        assert executed(db)[2] == [("where", "user_id", "==", "u"), ("where", "has_card_vector", "==", True),
                                   ("where", "card_rank", "==", 3), ("where", "modified_at", "IS_NULL", None),
                                   ("order_by", "item_id", "ASCENDING"), ("limit", 1), PROJECTION]


# -- search reads ---------------------------------------------------------------------------------

class TestSearchReads:
    async def test_find_nearest_prefilters_user_and_returns_distances(self, repo, db):
        db.cols[RECS].respond = lambda ops: [snap({**rec_doc("a"), "card_vector": Vector([1.0]),
                                                   "card_distance": 0.25})]
        hits = await repo.find_nearest_cards("u", [1, 0], 50)
        assert [(r.item_id, d) for r, d in hits] == [("a", 0.25)]
        (ops,) = executed(db)
        assert ops[0] == ("where", "user_id", "==", "u")
        assert ops[1] == ("select", (*RECORD_FIELDS, CARD_DISTANCE))   # the projection comes before the search
        kind, kw = ops[2]
        assert kind == "find_nearest" and kw == {
            "vector_field": "card_vector", "query_vector": Vector([1.0, 0.0]), "limit": 50,
            "distance_measure": DistanceMeasure.COSINE, "distance_result_field": "card_distance"}

    async def test_find_nearest_kind_prefilter(self, repo, db):
        await repo.find_nearest_cards("u", [1.0], 5, kind="image")
        assert executed(db)[0][:2] == [("where", "user_id", "==", "u"), ("where", "kind", "==", "image")]

    async def test_name_keys_array_contains_any_capped_at_30(self, repo, db):
        keys = [f"t:k{n:02d}" for n in range(40)] + ["t:k00"]
        await repo.find_by_name_keys("u", keys, 100, kind="pdf")
        assert executed(db) == [[("where", "user_id", "==", "u"), ("where", "kind", "==", "pdf"),
                                 ("where", "name_tokens", "array_contains_any", keys[:30]),
                                 ("order_by", "item_id", "ASCENDING"), ("limit", 100), PROJECTION]]

    async def test_no_name_keys_no_query(self, repo, db):
        assert await repo.find_by_name_keys("u", [], 100) == []
        assert executed(db) == []

    async def test_filter_only_newest_effective_date_first_without_dateless(self, repo, db):
        await repo.filter_records("u", None, None, None, 8)
        await repo.filter_records("u", "pdf", NOW - timedelta(days=7), NOW, 8)
        assert executed(db) == [
            [("where", "user_id", "==", "u"), ("where", "effective_date", ">=", TS_MIN),
             ("order_by", "effective_date", "DESCENDING"), ("limit", 8), PROJECTION],
            [("where", "user_id", "==", "u"), ("where", "kind", "==", "pdf"),
             ("where", "effective_date", ">=", NOW - timedelta(days=7)), ("where", "effective_date", "<=", NOW),
             ("order_by", "effective_date", "DESCENDING"), ("limit", 8), PROJECTION]]

    async def test_card_vectors_read_only_the_vector(self, repo, db):
        db.stored = {"u:a": {"item_id": "a", "card_vector": Vector([0.5, 0.5])}, "u:b": {"item_id": "b"}}
        assert await repo.card_vectors(U_SCOPE, ["a", "b", "zz"]) == {"a": [0.5, 0.5]}
        assert db.get_all_calls == [(["u:a", "u:b", "u:zz"], ["item_id", "card_vector"])]
