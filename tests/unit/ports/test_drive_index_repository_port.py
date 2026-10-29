"""Port contract test for DriveIndexRepository (USER_DRIVE_INDEXING_RFC §4)."""
import pytest

from src.ports.drive_index_repository import DriveIndexRepository

_EXPECTED = {
    "get_sync_state", "save_sync_state", "create_sync_state_if_absent", "update_sync_flags", "delete_sync_state",
    "acquire_lease",
    "release_lease", "list_sync_user_ids", "get_records", "upsert_records", "delete_records", "due_records",
    "oldest_due_at", "children", "records_page", "records_seen_before", "count_carded", "lowest_ranked_carded",
    "card_capped", "count_by_state", "delete_user_records", "find_nearest_cards", "find_by_name_keys",
    "filter_records", "card_vectors", "has_record_in",
    # chunks (Slice 2, §24.6): records and chunks stay in one repository
    "write_chunks", "delete_chunks_except", "find_nearest_chunks", "find_chunks_by_term", "get_chunks",
    "find_next_file_with_term",   # term selectivity probed past a full term page (§24.6, arbiter R2 S-M1)
    "get_chunks_with_vectors",   # folder-scoped chunk ranking in memory (§10.1 Folder; arbiter-accepted delta)
    "readable_chunk_total", "chunks_page", "delete_user_chunks",
    "chunk_capped", "count_unread",   # chunk-cap release (§5.4) and `unread` (§10.2): CORE-DELTA 2, arbiter-accepted
    "count_capped",   # the fan-out's cap alert (§5.4, drive_cap_alert): files held by each per-user cap
    # Search v2 (§25.13.1): abstract from Part 0, stubbed in both adapters until P2
    "find_by_about_keys", "find_expiring", "find_chunks_by_word_keys", "get_chunk_keys", "find_hints_by_keys",
    "get_hint_keys", "find_nearest_hints", "get_hints", "get_hints_with_vectors", "write_hints",
    "delete_hints_except", "delete_user_hints", "hints_page", "enrich_queue", "enrich_wakeups", "count_by_job_state",
    "keys_pending_page", "count_describe_gaps", "get_records_lean",   # XC-3/XC-9
    "get_model_usage", "add_model_usage", "get_initial_build",
    "reserve_model_usd",   # §27.10 (Δ106, G9): month-scoped transcription reservations
    "spend_initial_build", "close_initial_build",
    # files the bot cannot open (§26.2, D33): the tick's counters and the fan-out's notice read the marked records;
    # the fan-out alone writes its notice flag (Δ54)
    "inaccessible_records", "set_inaccessible_notified",
    # transcripts (§27.3-§27.5, §27.8 b; plan Task 7A): jobs + window results, provider batches, the sharded store
    "get_transcript_job", "save_transcript_job", "delete_transcript_job", "save_window_result", "get_window_results",
    "create_batch", "save_batch", "delete_batch", "get_open_batches",
    "get_transcript", "put_transcript", "touch_transcript", "delete_transcript",
    # transcripts (plan Task 7B): the record queues, the in-flight copy check (G12), the store's orphans, the purge
    # helpers, and the collection-group orphan sweep kept apart from them (ruling T-R14)
    "transcript_queue", "transcript_wakeups", "jobs_by_content_key", "orphan_transcripts",
    "delete_user_transcript_jobs", "delete_user_batches", "delete_user_transcripts", "delete_user_transcript_orphans",
    # transcript stage A (plan Task 12): in-flight `working` jobs and copy-waiting records, which neither queue returns
    "transcript_records",
    # transcript stage B (plan Task 13): the orphan sweep keeps a stored transcript a record still carries (§27.8 b)
    "transcript_key_in_use",
    # the transcript mirror (§27.9.4, Δ187, Δ217; plan T2 Task 20A): the records' mirror queue and its wake-ups, and the
    # scanning purge that keeps the store entries the mirror ledger pins (Δ186, Δ214)
    "mirror_queue", "mirror_wakeups", "delete_user_transcripts_except",
    # a folder past FOLDER_INMEM_CHUNKS: kNN restricted to the walked files' ids (RFC Δ246)
    "find_nearest_chunks_in", "find_nearest_hints_in",
}


def test_abstract_methods_are_exactly_the_contract():
    assert set(DriveIndexRepository.__abstractmethods__) == _EXPECTED


def test_cannot_instantiate():
    with pytest.raises(TypeError):
        DriveIndexRepository()  # type: ignore[abstract]
