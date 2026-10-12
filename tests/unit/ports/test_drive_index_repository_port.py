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
    "get_chunks_with_vectors",   # folder-scoped chunk ranking in memory (§10.1 Folder; arbiter-accepted delta)
    "readable_chunk_total", "chunks_page", "delete_user_chunks",
    "chunk_capped", "count_unread",   # chunk-cap release (§5.4) and `unread` (§10.2): CORE-DELTA 2, arbiter-accepted
}


def test_abstract_methods_are_exactly_the_contract():
    assert set(DriveIndexRepository.__abstractmethods__) == _EXPECTED


def test_cannot_instantiate():
    with pytest.raises(TypeError):
        DriveIndexRepository()  # type: ignore[abstract]
