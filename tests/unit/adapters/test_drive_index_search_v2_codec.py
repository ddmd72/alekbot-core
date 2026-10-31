"""Search v2 fields through the drive index codecs (USER_DRIVE_INDEXING_RFC §25.2.1, §25.2.2, §25.4.6, §25.13.3): every
v2 record field, the sync state's per-agent backoff and the chunk keys round-trip through Firestore-shaped documents;
the v1 stream projections leave the chunk keys out; the in-memory and Firestore repositories read chunks alike."""
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds

from src.adapters.firestore_drive_index_repo import (
    CHUNK_FIELDS, CHUNK_VECTOR, RECORD_FIELDS, STREAM_FIELDS, chunk_from_doc, chunk_to_doc, record_from_doc,
    record_to_doc, state_from_doc, state_to_doc,
)
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_enrichment import DriveProfile, KeyValue, PhotoObject, ProfileDate, ProfileEntity
from src.domain.drive_index import DriveChunk, DriveIndexRecord, DriveSyncState, chunk_doc_id

from tests.unit.adapters.firestore_drive_chunk_fakes import ChunkDb, make_chunk_repo
from tests.unit.services.drive_index_fakes import U_SCOPE

T = datetime(2026, 10, 13, 9, 30, 15, 250000, tzinfo=timezone.utc)

V2_RECORD_FIELDS = (
    "profile", "valid_until", "profile_source", "profile_input_fingerprint", "profile_fingerprint", "profile_version",
    "profiled_content_version", "profile_state", "profile_defer_until", "profile_parked_month", "profile_hold",
    "profile_hold_version", "profile_attempts", "passages_state", "passages_defer_until", "passages_parked_month",
    "passages_hold", "passages_hold_version", "passages_attempts", "passages_generation", "passages_next_index",
    "passages_done_at", "enrich_ready", "enrich_wake_at", "effective_day", "keys_pending", "keys_version",
    "keys_capped", "about_keys", "place_forms", "photo_description", "photo_objects", "photo_scene", "visible_text",
    "readme_excerpt", "readme_excerpt_version",
)


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


def profile() -> DriveProfile:
    return DriveProfile(
        title="Colosseum entry", what_it_is="Two entry tickets", language="en", doc_type="ticket",
        life_domains=("travel",), dates=(ProfileDate("event", date(2026, 5, 12)), ProfileDate("issued", date(2026, 4, 1))),
        entities=(ProfileEntity("Rome", "place", "Roma", ("Rome", "Рим")),),
        key_values=(KeyValue("ticket_number", "No. 0042", "0042"),), topics=("sightseeing",),
        topics_native=("turismo",), topic_words=("sightseeing", "туризм"), label_words=("ticket", "квиток"),
        domain_words=("trip",), validity_words=("valid until",))


def v2_record() -> DriveIndexRecord:
    """Every v2 field away from its default."""
    return DriveIndexRecord(
        user_id="u", item_id="i", profile=profile(), valid_until=T.replace(hour=0, minute=0, second=0, microsecond=0),
        profile_source="model", profile_input_fingerprint="fp2", profile_fingerprint="fp1", profile_version=1,
        profiled_content_version="c1", profile_state="ready", profile_defer_until=T + timedelta(hours=1),
        profile_parked_month="2026-10", profile_hold="refused", profile_hold_version="1:fp1", profile_attempts=2,
        passages_state="deferred", passages_defer_until=T + timedelta(hours=2), passages_parked_month="2026-09",
        passages_hold="failed", passages_hold_version="1:4", passages_attempts=1, passages_generation=4,
        passages_next_index=6, passages_done_at=T - timedelta(days=1), enrich_ready=True,
        enrich_wake_at=T + timedelta(minutes=5), effective_day="2026-05-12", keys_pending=True, keys_version=1,
        keys_capped=True, about_keys=("e:rome", "k:pdf"), place_forms=("Rome", "Roma"),
        photo_description="a fountain at night", photo_objects=(PhotoObject("fountain", ("fuente", "фонтан")),),
        photo_scene="city square", visible_text="Piazza", readme_excerpt="# Project", readme_excerpt_version="c9")


class TestRecordFields:
    def test_the_fixture_sets_every_v2_field(self):
        default = DriveIndexRecord(user_id="u", connection_id="u", item_id="i")
        assert [n for n in V2_RECORD_FIELDS if getattr(v2_record(), n) == getattr(default, n)] == []

    def test_every_v2_field_is_a_stored_field(self):
        assert set(V2_RECORD_FIELDS) <= set(RECORD_FIELDS)

    def test_round_trip_through_stored_types(self):
        assert record_from_doc(stored(record_to_doc(v2_record()))) == v2_record()

    def test_profile_dates_are_iso_strings_in_the_document(self):
        doc = record_to_doc(v2_record())
        assert doc["profile"]["dates"][0] == {"label": "event", "iso": "2026-05-12", "printed": ""}
        assert doc["photo_objects"] == [{"name": "fountain", "forms": ["fuente", "фонтан"]}]

    @pytest.mark.parametrize("name", ["valid_until", "profile_defer_until", "passages_defer_until",
                                      "passages_done_at", "enrich_wake_at"])
    def test_timestamps_read_back_as_plain_utc(self, name):
        back = getattr(record_from_doc(stored(record_to_doc(v2_record()))), name)
        assert type(back) is datetime and back.utcoffset() == timedelta(0)

    def test_neutral_defaults(self):
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i")
        assert rec.profile is None and rec.profile_state == "none" and rec.passages_state == "none"
        assert rec.about_keys == () and rec.keys_version == 0 and not rec.enrich_ready and not rec.keys_capped

    def test_a_v1_document_reads_with_the_defaults(self):
        v1 = {f: v for f, v in record_to_doc(DriveIndexRecord(user_id="u", connection_id="u", item_id="i")).items()
              if f not in V2_RECORD_FIELDS}
        assert record_from_doc(v1) == DriveIndexRecord(user_id="u", connection_id="u", item_id="i")


class TestStateField:
    def test_backoff_by_agent_round_trips_as_plain_utc(self):
        state = DriveSyncState(user_id="u", connection_id="u", model_backoff_by_agent={"drive_profiler": T, "drive_passages": T})
        back = state_from_doc("u", stored(state_to_doc(state)))
        assert back == state
        assert all(type(v) is datetime for v in back.model_backoff_by_agent.values())

    def test_default_is_empty(self):
        assert DriveSyncState(user_id="u", connection_id="u").model_backoff_by_agent == {}
        assert state_from_doc("u", {}).model_backoff_by_agent == {}


class TestChunkKeys:
    def test_round_trip_with_keys(self):
        c = DriveChunk("u", "i", 3, 7, "у Коїмбрі", 0, 9, "pdf", ("1250",), ("w:коїмбр", "m:2023-03"), 1)
        assert chunk_from_doc(chunk_to_doc(c)) == c

    def test_missing_keys_read_as_empty(self):
        c = DriveChunk("u", "i", 3, 7, "t", 0, 1, "pdf")
        doc = {k: v for k, v in chunk_to_doc(c).items() if k not in ("word_keys", "keys_version")}
        assert chunk_from_doc(doc) == c

    def test_v1_streams_do_not_project_the_keys(self):
        assert "word_keys" in CHUNK_FIELDS and "keys_version" in CHUNK_FIELDS
        assert "word_keys" not in STREAM_FIELDS and "keys_version" not in STREAM_FIELDS


KEYED = DriveChunk("u", "a", 1, 0, "text", 0, 4, "pdf", ("1250",), ("w:text",), 1, connection_id="u")


class TestReadParity:
    async def test_get_chunks_returns_the_keys_in_both(self, mock_env_config):
        memory = InMemoryDriveIndexRepository()
        await memory.write_chunks([KEYED], [[0.5, 0.5]])
        db = ChunkDb()
        db.stored[chunk_doc_id(U_SCOPE, "a", 1, 0)] = chunk_to_doc(KEYED)
        firestore = make_chunk_repo(db, mock_env_config)
        assert await memory.get_chunks(U_SCOPE, "a", 1, [0]) == await firestore.get_chunks(U_SCOPE, "a", 1, [0]) == [KEYED]

    async def test_terms_false_leaves_the_keys_out_in_both(self, mock_env_config):
        memory = InMemoryDriveIndexRepository()
        await memory.write_chunks([KEYED], [[0.5, 0.5]])
        ((c, _),) = await memory.get_chunks_with_vectors(U_SCOPE, "a", 1, [0], terms=False)
        assert c == replace(KEYED, content_terms=(), word_keys=())
        db = ChunkDb()
        db.stored[chunk_doc_id(U_SCOPE, "a", 1, 0)] = {**chunk_to_doc(KEYED), CHUNK_VECTOR: [0.5, 0.5]}
        await make_chunk_repo(db, mock_env_config).get_chunks_with_vectors(U_SCOPE, "a", 1, [0], terms=False)
        ((_, paths),) = db.get_all_calls
        assert "word_keys" not in paths and "content_terms" not in paths and "keys_version" in paths


def test_every_record_field_has_a_codec_path():
    """Each record field survives the codec at its non-default value (the generic writer + a reader where needed)."""
    rec = v2_record()
    back = record_from_doc(stored(record_to_doc(rec)))
    assert [f.name for f in fields(DriveIndexRecord) if getattr(back, f.name) != getattr(rec, f.name)] == []
