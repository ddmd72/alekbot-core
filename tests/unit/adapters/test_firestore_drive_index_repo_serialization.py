"""FirestoreDriveIndexRepository serialization (USER_DRIVE_INDEXING_RFC §3.2, §3.5) and the write encoding,
proven on the real google-cloud-firestore client offline: the batch is built by the SDK and captured at commit."""
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore
from google.cloud.firestore_v1.async_batch import AsyncWriteBatch
from google.cloud.firestore_v1.async_document import AsyncDocumentReference
from google.cloud.firestore_v1.async_transaction import AsyncTransaction

from src.adapters.firestore_drive_index_repo import (
    RECORD_FANOUT_FIELDS, RECORD_FIELDS, STATE_FIELDS, FirestoreDriveIndexRepository, record_from_doc, record_to_doc, state_from_doc,
    state_to_doc,
)
from src.domain.drive_enrichment import DriveProfile, GradedValue, KeyValue, PhotoObject, ProfileDate, ProfileEntity
from src.domain.drive_index import (
    ContentClass, DocumentBrief, DriveIndexRecord, DriveSyncState, EpochPhase, ErrorCategory, Reason, RecordState,
    Scope, TriageVerdict,
)
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_note_check import NoteCheck
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 11, 12, 30, 15, 123456, tzinfo=timezone.utc)


def full_record() -> DriveIndexRecord:
    """Every field away from its default, so a dropped or mistyped field fails the round trip."""
    return DriveIndexRecord(
        user_id="u1", item_id="I1", parent_id="P1", name="Договор_ред3.docx", is_folder=True, size_bytes=10,
        mime_type="application/pdf", web_url="https://example.invalid/i1", child_count=3,
        media=DriveMediaFacts(taken_at=datetime(2025, 8, 1, 10, tzinfo=timezone.utc), camera_make="Apple",
                              latitude=41.9, longitude=12.5),
        kind="pdf", content_class=ContentClass.DOCUMENT, scope=Scope.NORMAL, state=RecordState.FAILED,
        reason=Reason.EXTRACT_ERROR, content_version="c1", modified_at=T, fs_modified_at=T - timedelta(days=1),
        effective_date=T - timedelta(days=2), place_name="Rome / Roma", geo_table_version=1, policy_version=2,
        extraction_version=3, card_version=4, embedding_space="gemini-embedding-2/768", card_hash="h",
        has_card=True, card_capped=True, card_refused=True, card_rank=1, name_tokens=("t:abc", "w:a"),
        name_keys_version=6, content_identity=ContentIdentity("quickxor", "q", 10),
        dirty_since=T - timedelta(hours=1), due_at=T + timedelta(minutes=5), priority=4, attempts=2,
        attempt_started_at=T - timedelta(minutes=1), probe_pending=True, rescope_pending=True, cascade_cursor="K",
        probed_child_count=600, reobserve_pending=True, reobserve_cursor="next-page", index_generation=5,
        indexed_generation=4, indexed_version="c0", extracted_by="markitdown@1", text_chars=100, pages_total=2,
        chunk_count=3, chunk_space="old-space/768", chunk_reembed_pending=True, truncated=True,
        lead_text="Договор поставки",
        brief=DocumentBrief(doc_type="contract", description="Supply contract", doc_date=T - timedelta(days=30),
                            parties=("ACME", "Мельник"), amount="1 250.00 EUR"),
        triage_verdict=TriageVerdict.PHOTO, triage_version=1, triaged_content_version="c1",
        caption="a beach at sunset", defer_until=T + timedelta(hours=2), defer_priority=3, seen_epoch=7,
        readme_pending=True, readme_item_id="RM1", readme_version="c7", chunks_refused_space="new-space/3072",
        model_outages=2, model_read_at=T - timedelta(hours=3),
        # Search v2 (§25.2.1): every field, the nested profile with every one of its own fields set
        profile=DriveProfile(
            title="Boarding pass Lisbon–Rome", what_it_is="An airline boarding pass", language="es",
            doc_type="ticket", life_domains=("travel",), dates=(ProfileDate("valid_until", date(2026, 5, 12), "12 05 2026"),),
            entities=(ProfileEntity("Roma", "place", "ROMA FCO", ("rome", "рим")),),
            key_values=(KeyValue("reference", "AB 12-34", "ab1234"),), topics=("flight",), topics_native=("vuelo",),
            topic_words=("flight", "flights"), label_words=("ticket",), domain_words=("travel",),
            validity_words=("valid",),
            graded=(GradedValue("value", "AB 12-34", "in_text_as", "AB1234"),)),   # §26.4 (Δ59)
        valid_until=datetime(2026, 5, 12, tzinfo=timezone.utc), profile_source="model",
        profile_input_fingerprint="pif", profile_fingerprint="pf", profile_version=2, profiled_content_version="c1",
        profile_state="current", profile_defer_until=T + timedelta(hours=4), profile_parked_month="2026-09",
        profile_hold="refused", profile_hold_version="1:pif", profile_attempts=1, passages_state="deferred",
        passages_defer_until=T + timedelta(hours=5), passages_parked_month="2026-08", passages_hold="failed",
        passages_hold_version="1:4", passages_attempts=3, passages_generation=4, passages_next_index=2,
        passages_done_at=T - timedelta(hours=6), passages_version=1, enrich_ready=True, enrich_wake_at=T + timedelta(hours=7),
        effective_day="2026-10-09", describe_gap="held",   # XC-3: a non-default derived gap
        keys_pending=True, keys_version=1, keys_capped=True,
        about_keys=("e:rome", "y:2026"), place_forms=("Rome", "Roma"), photo_description="a fountain in a square",
        photo_objects=(PhotoObject("fountain", ("fuente", "фонтан")),), photo_scene="city square",
        visible_text="TREVI", readme_excerpt="# Project", readme_excerpt_version="c7",
        script_rendered=True,
        capture="scan", capture_source="detected",   # §26.3 (D34)
        photo_scene_labels=("sunset", "beach"),   # D40
        card_accuracy=88, accuracy_version=1, pages_refused=2, read_by_fallback=True,   # §26.4 (Δ61)
        # §26.2 (D33, Δ47, Δ68): files the bot cannot open; the last is the fan-out's own field (Δ54)
        inaccessible_since=T - timedelta(days=4), inaccessible_checked_at=T - timedelta(hours=8),
        inaccessible_reobserved=True, inaccessible_notified_since=T - timedelta(days=5),
        # §27 (transcripts): plain fields, strings for states and holds (Δ47)
        transcript_state="waiting", transcript_key="q:4096", transcript_version=1,
        transcript_wake_at=T + timedelta(hours=9), transcript_attempts=2, transcript_hold="budget",
        transcript_hold_version="1:q:4096", transcript_stub="too_long", transcript_seconds=3725,
        transcript_speakers=3, transcript_languages=("uk", "en"), transcript_body_version=1,
        transcript_truncated=True,
        # §27.9.4 (the mirror queue, Δ217): the flag, the stamped digest and a held record's hold
        mirror_due=True, mirrored_digest="md1", mirror_retry_at=T + timedelta(hours=10), mirror_held_digest="mh1",
        # DRIVE_FILE_NOTES_RFC §3.3: the record's copy of the owner's note (part B review verdict, joint for A, B, C)
        owner_note_id="N1", owner_note_version=2, owner_note_text="Рахунок підрядника, 1.048,45 EUR", owner_doc_type="contract",
        owner_life_areas=("home", "finance"), owner_note_merge_version=1, note_lookup_due=True,
        # MULTI_DRIVE_RFC §7.2, §8: the connection and the adapter-built item identity
        connection_id="c1", identity="microsoft_onedrive:D1:I1")


def full_state() -> DriveSyncState:
    return DriveSyncState(
        user_id="u1", connection_id="c1", drive_identity="microsoft_onedrive:D1", root_item_id="R", cursor="cur", round_complete=True, round_started_at=T,
        epoch=2, epoch_phase=EpochPhase.SWEEPING, initial_enumeration_done=True, purging=True,
        created_at=T - timedelta(days=3), lease_owner="tick", lease_until=T + timedelta(minutes=31), continuation_enqueued_at=T,
        applied_versions={"policy": 1}, target_versions={"policy": 2}, applied_space="old/768",
        target_space="new/3072", upgrade_cursor="I9", upgrade_found_stale=True,
        observed_as_of=T, access_lost_at=T, last_success_at=T, last_error=ErrorCategory.THROTTLED,
        stale_alerted=True, reconnect_notified=True, systemic_failures=2, counters={"indexed": 3},
        model_backoff_until=T + timedelta(minutes=10), model_failures=3, usage_month="2026-10", model_pages_used=40,
        triage_images_used=12, upgrade_parked_month="2026-10",
        model_backoff_by_agent={"drive_reader": T + timedelta(minutes=20), "drive_profiler": T + timedelta(minutes=5)},
        described_count=7, described_grew_at=T - timedelta(hours=2), migration_alerted=True, stall_alerted=True,
        fallback_alerted_day="2026-10-09", purge_keep_transcripts=True,
        card_cap_alerted=True, chunk_cap_alerted=True,   # the fan-out's cap alert flags (drive_cap_alert)
        disconnected_by_user_at=T - timedelta(days=1),   # R1's deliberate-disconnect mark (SECRETS_AT_REST_RFC §6.2 F1)
        # DRIVE_FILE_NOTES_RFC §3.4: the notes step's state (part B review verdict, plus the rollback stamp)
        notes_applied_at=T - timedelta(minutes=4), notes_applied_id="N7", notes_paging=True,
        notes_scanned_at=T - timedelta(minutes=3), notes_merge_version=1, notes_space="old/768",
        notes_space_at=T - timedelta(minutes=2), notes_space_id="N3", notes_tick_at=T - timedelta(minutes=1),
        # §3.4, §7.1, §7.4: the archive checks, the audit's clock and pass cursor (part C review verdict)
        note_checks=(NoteCheck("n1", "I1", "gone", T),), notes_audited_at=T, notes_audit_after_at=T,
        notes_audit_after_id="n1")


def stored(value):
    """What Firestore hands back: timestamps as DatetimeWithNanoseconds, everything else as written."""
    if isinstance(value, datetime):
        return DatetimeWithNanoseconds(value.year, value.month, value.day, value.hour, value.minute, value.second,
                                       value.microsecond, tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {k: stored(v) for k, v in value.items()}
    if isinstance(value, list):
        return [stored(v) for v in value]
    return value


class TestRecordSerialization:
    def test_fixture_sets_every_field(self):
        default = DriveIndexRecord(user_id="x", item_id="y")
        same = [f.name for f in fields(DriveIndexRecord) if getattr(full_record(), f.name) == getattr(default, f.name)]
        assert same == []

    def test_doc_holds_every_field_as_plain_values(self):
        doc = record_to_doc(full_record())
        assert set(doc) == set(RECORD_FIELDS)
        # the fan-out's own field is never part of a record write (§26.2, Δ54); every other field is
        assert not set(RECORD_FANOUT_FIELDS) & set(doc)
        assert set(doc) | set(RECORD_FANOUT_FIELDS) == {f.name for f in fields(DriveIndexRecord)}
        assert doc["content_class"] == "document" and doc["scope"] == "normal"
        assert doc["state"] == "failed" and doc["reason"] == "extract_error"
        assert doc["name_tokens"] == ["t:abc", "w:a"] and type(doc["priority"]) is int
        assert doc["media"]["camera_make"] == "Apple" and isinstance(doc["media"], dict)
        assert doc["content_identity"] == {"family": "quickxor", "digest": "q", "size": 10}
        assert doc["modified_at"] == T and doc["modified_at"].tzinfo is not None

    def test_round_trip_through_stored_types(self):
        # the fan-out's field is written by its own field update (set_inaccessible_notified, §26.2, Δ54), not by
        # record_to_doc: the stored document holds both, and record_from_doc reads every field back
        fanout = {name: getattr(full_record(), name) for name in RECORD_FANOUT_FIELDS}
        back = record_from_doc(stored({**record_to_doc(full_record()), **fanout}))
        assert back == full_record()

    def test_read_datetimes_are_plain_utc(self):
        back = record_from_doc(stored(record_to_doc(full_record())))
        for name in ("modified_at", "fs_modified_at", "effective_date", "dirty_since", "due_at", "attempt_started_at",
                     "model_read_at", "transcript_wake_at"):
            value = getattr(back, name)
            assert type(value) is datetime and value.utcoffset() == timedelta(0), name

    def test_missing_fields_take_defaults_and_extras_are_ignored(self):
        rec = record_from_doc({"user_id": "u", "item_id": "i", "card_vector": [1.0], "has_card_vector": True,
                               "card_distance": 0.1, "reason": None, "priority": None})
        assert rec == DriveIndexRecord(user_id="u", item_id="i")


class TestStateSerialization:
    def test_fixture_sets_every_field(self):
        default = DriveSyncState(user_id="x")
        same = [f.name for f in fields(DriveSyncState) if getattr(full_state(), f.name) == getattr(default, f.name)]
        assert same == []

    def test_doc_excludes_fanout_flags(self):
        doc = state_to_doc(full_state())
        # Search v2 (§25.8) adds three fan-out flags, written only by update_sync_flags like the v1 two
        # the cap alert (drive_cap_alert) adds two more, written the same way; R1's disconnect mark one more (F1)
        flags = {"stale_alerted", "reconnect_notified", "migration_alerted", "stall_alerted", "fallback_alerted_day",
                 "card_cap_alerted", "chunk_cap_alerted", "disconnected_by_user_at"}
        assert set(doc) == {f.name for f in fields(DriveSyncState)} - flags
        assert set(STATE_FIELDS) == set(doc) - {"lease_owner", "lease_until"}   # the lease is not save_sync_state's
        assert not flags & set(doc)
        assert doc["described_count"] == 7 and doc["described_grew_at"] == T - timedelta(hours=2)
        assert doc["epoch_phase"] == "sweeping" and doc["last_error"] == "throttled"
        assert doc["purging"] is True and doc["systemic_failures"] == 2 and doc["created_at"] == T - timedelta(days=3)
        assert doc["applied_versions"] == {"policy": 1} and "pending_reobserve" not in doc

    def test_round_trip_with_flags_from_the_document(self):
        doc = {**stored(state_to_doc(full_state())), "stale_alerted": True, "reconnect_notified": True,
               "migration_alerted": True, "stall_alerted": True, "fallback_alerted_day": "2026-10-09",
               "card_cap_alerted": True, "chunk_cap_alerted": True,
               "disconnected_by_user_at": stored(T - timedelta(days=1))}
        assert state_from_doc("u1", doc) == full_state()

    def test_missing_doc_fields_default(self):
        assert state_from_doc("u1", {"user_id": "u1"}) == DriveSyncState(user_id="u1")


# -- the SDK's own encoding of our writes ---------------------------------------------------------

@pytest.fixture
def sdk(monkeypatch, mock_env_config):
    """A real AsyncClient that never reaches the network: commit captures the Write protos it would send."""
    sent = []

    async def capture(self, *args, **kwargs):
        sent.extend(self._write_pbs)
        return [object() for _ in self._write_pbs]   # DocumentReference.set reads the first result

    monkeypatch.setattr(AsyncWriteBatch, "commit", capture)
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config), sent


class TestWriteEncoding:
    async def test_record_write_replaces_maps_whole_and_keeps_the_vector(self, sdk):
        repo, sent = sdk
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="i", has_card=True,
                                                    media=DriveMediaFacts(image_width=10))])
        write = sent[0]
        assert write.update.name.endswith("/documents/test_drive_index_records/u:i")
        assert set(write.update_mask.field_paths) == set(RECORD_FIELDS)   # top-level paths only
        assert "media.image_width" not in write.update_mask.field_paths   # the map is replaced, stale keys go
        assert set(write.update.fields["media"].map_value.fields) == set(DriveMediaFacts().to_dict())
        assert write.update.fields["media"].map_value.fields["image_width"].integer_value == 10
        assert "card_vector" not in write.update_mask.field_paths          # not listed: the stored vector stays

    async def test_given_vector_is_written_as_a_vector_value(self, sdk):
        repo, sent = sdk
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="i", has_card=True)], {"i": [0.5, 0.25]})
        write = sent[0]
        assert {"card_vector", "has_card_vector"} <= set(write.update_mask.field_paths)
        vec = write.update.fields["card_vector"].map_value.fields
        assert vec["__type__"].string_value == "__vector__"
        assert [v.double_value for v in vec["value"].array_value.values] == [0.5, 0.25]
        assert write.update.fields["has_card_vector"].boolean_value is True

    async def test_cardless_record_deletes_the_vector(self, sdk):
        repo, sent = sdk
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="i", has_card=False)])
        write = sent[0]
        assert "card_vector" in write.update_mask.field_paths and "card_vector" not in write.update.fields
        assert write.update.fields["has_card_vector"].boolean_value is False

    async def test_default_media_still_replaces_the_stored_map(self, sdk):
        repo, sent = sdk
        await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="i")])
        assert "media" in sent[0].update_mask.field_paths
        fields_ = sent[0].update.fields["media"].map_value.fields
        assert set(fields_) == set(DriveMediaFacts().to_dict()) and fields_["camera_make"].string_value == ""

    async def test_save_sync_state_never_masks_the_flags(self, tx_sdk):
        """The save is a fenced transaction (§7.1): the holder's write is encoded by the SDK's transaction."""
        repo, stored, sent, _ = tx_sdk
        stored["c1"] = state_to_doc(full_state())   # the state's document id is its connection id (§8)
        await repo.save_sync_state(full_state(), full_state().lease_owner)
        mask = set(sent[0].update_mask.field_paths)
        assert mask == set(STATE_FIELDS)
        assert "stale_alerted" not in mask and "reconnect_notified" not in mask
        assert not {"migration_alerted", "stall_alerted", "fallback_alerted_day"} & mask   # Search v2 flags (§25.8)
        assert not {"card_cap_alerted", "chunk_cap_alerted"} & mask   # the cap alert's flags (drive_cap_alert)


@pytest.fixture
def compiled(monkeypatch, mock_env_config):
    """A real AsyncClient whose query `get`/`stream` compile the request proto instead of calling the server."""
    from google.cloud.firestore_v1.async_aggregation import AsyncAggregationQuery
    from google.cloud.firestore_v1.async_query import AsyncQuery
    from google.cloud.firestore_v1.async_vector_query import AsyncVectorQuery

    protos = []

    async def get(self, *args, **kwargs):
        proto = self._to_protobuf()
        protos.append(proto)
        orders = [(o.field.field_path, o.direction.name) for o in getattr(proto, "order_by", [])]
        if orders[:1] == [("card_rank", "DESCENDING")]:   # a carded record exists: eviction runs all three queries
            doc = record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="x", card_rank=2))
            return [SimpleNamespace(to_dict=lambda: doc)]
        return []

    async def stream(self, *args, **kwargs):
        protos.append(self._to_protobuf())
        for _ in ():
            yield

    async def count_get(self, *args, **kwargs):
        protos.append(self._to_protobuf())
        return [[SimpleNamespace(value=0)]]

    monkeypatch.setattr(AsyncQuery, "get", get)
    monkeypatch.setattr(AsyncQuery, "stream", stream)
    monkeypatch.setattr(AsyncVectorQuery, "get", get)
    monkeypatch.setattr(AsyncAggregationQuery, "get", count_get)
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config), protos


class TestQueriesCompile:
    """Every query chain the adapter builds is accepted by the SDK (no server)."""

    async def test_every_read_compiles(self, compiled):
        repo, protos = compiled
        await repo.due_records(U_SCOPE, T, 10)
        await repo.oldest_due_at(U_SCOPE, 0)
        await repo.children(U_SCOPE, "p", "a", 5)
        await repo.records_page(U_SCOPE, "a", 5)
        await repo.records_seen_before(U_SCOPE, 2, 5)
        await repo.count_by_state(U_SCOPE)
        await repo.lowest_ranked_carded(U_SCOPE)
        await repo.card_capped(U_SCOPE, 5)
        await repo.delete_user_records(U_SCOPE, 5)
        await repo.list_sync_scopes()
        await repo.find_nearest_cards(U_SCOPE, [0.1] * 768, 50, kind="pdf")
        await repo.find_by_name_keys(U_SCOPE, ["w:a", "t:abc"], 100, kind="pdf")
        await repo.filter_records(U_SCOPE, "pdf", T - timedelta(days=1), T, 8)
        # due_records: one per class (5); count_by_state: 6 states + carded (7); lowest_ranked_carded: 3
        assert len(protos) == 25

    async def test_undated_eviction_query_is_a_null_filter(self, compiled):
        repo, protos = compiled
        await repo.lowest_ranked_carded(U_SCOPE)
        undated = protos[2].where.composite_filter.filters
        assert any(f.unary_filter.field.field_path == "modified_at" and f.unary_filter.op.name == "IS_NULL"
                   for f in undated)
        assert [(o.field.field_path, o.direction.name) for o in protos[2].order_by] == [("item_id", "ASCENDING")]

    async def test_keys_only_projection_encodes(self, compiled):
        repo, protos = compiled
        await repo.list_sync_scopes()
        assert [f.field_path for f in protos[0].select.fields] == ["user_id"]


# -- the lease through the SDK's own transaction wrapper -------------------------------------------

@pytest.fixture
def tx_sdk(monkeypatch, mock_env_config):
    """A real AsyncClient and the real `@async_transactional`. Begin, commit and the document read are stubbed;
    the Write protos the transaction would commit are captured."""
    stored, sent, reads = {}, [], []

    async def begin(self, retry_id=None):
        self._id = b"tx"

    async def commit(self):
        sent.extend(self._write_pbs)
        self._clean_up()
        return []

    async def get(self, field_paths=None, transaction=None, **kwargs):
        reads.append(transaction)
        data = stored.get(self.id)
        return SimpleNamespace(exists=data is not None, to_dict=lambda: dict(data) if data is not None else None)

    monkeypatch.setattr(AsyncTransaction, "_begin", begin)
    monkeypatch.setattr(AsyncTransaction, "_commit", commit)
    monkeypatch.setattr(AsyncDocumentReference, "get", get)
    client = firestore.AsyncClient(project="p", credentials=AnonymousCredentials(), database="d")
    mock_env_config.drive_index_records_collection = "test_drive_index_records"
    mock_env_config.drive_sync_state_collection = "test_drive_sync_state"
    return FirestoreDriveIndexRepository(client, mock_env_config), stored, sent, reads


def _is_null(value) -> bool:
    return value._pb.WhichOneof("value_type") == "null_value"


class TestLeaseEncoding:
    async def test_create_writes_the_state_without_flags_inside_the_transaction(self, tx_sdk):
        repo, _, sent, reads = tx_sdk
        assert (await repo.acquire_lease(U_SCOPE, "a", T, T + timedelta(minutes=31), create=True)).lease_owner == "a"
        assert isinstance(reads[0], AsyncTransaction)
        (write,) = sent
        assert write.update.name.endswith("/documents/test_drive_sync_state/u")
        assert write.update.fields["lease_owner"].string_value == "a"
        assert "stale_alerted" not in write.update.fields and "reconnect_notified" not in write.update.fields

    async def test_taking_an_expired_lease_updates_only_the_lease_fields(self, tx_sdk):
        repo, stored, sent, _ = tx_sdk
        stored["u"] = {**state_to_doc(DriveSyncState(user_id="u", connection_id="u", epoch=4, lease_owner="b", lease_until=T)),
                       "stale_alerted": True}
        await repo.acquire_lease(U_SCOPE, "a", T, T + timedelta(minutes=31), create=True)
        (write,) = sent
        assert set(write.update_mask.field_paths) == {"lease_owner", "lease_until"}
        assert write.current_document.exists is True   # update(): never creates the document

    async def test_busy_and_missing_without_create_commit_no_write(self, tx_sdk):
        repo, stored, sent, _ = tx_sdk
        assert await repo.acquire_lease(U_SCOPE, "a", T, T + timedelta(minutes=31), create=False) is None
        stored["u"] = state_to_doc(DriveSyncState(user_id="u", connection_id="u", lease_owner="b", lease_until=T + timedelta(minutes=1)))
        assert await repo.acquire_lease(U_SCOPE, "a", T, T + timedelta(minutes=31), create=True) is None
        assert sent == []

    async def test_release_by_owner_nulls_the_lease_fields(self, tx_sdk):
        repo, stored, sent, _ = tx_sdk
        stored["u"] = state_to_doc(DriveSyncState(user_id="u", connection_id="u", lease_owner="a", lease_until=T))
        await repo.release_lease(U_SCOPE, "a")
        await repo.release_lease(U_SCOPE, "someone-else")   # stored doc still names "a": no write
        (write,) = sent
        assert set(write.update_mask.field_paths) == {"lease_owner", "lease_until"}
        assert _is_null(write.update.fields["lease_owner"]) and _is_null(write.update.fields["lease_until"])
