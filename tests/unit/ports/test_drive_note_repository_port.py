"""Port contract test for DriveNoteRepository (DRIVE_FILE_NOTES_RFC §3, §4.4, §4.8, §5.1, §6.4, §7): the S1 core declares
every operation S2 and S3 need, so later slices add no abstract method."""
import pytest

from src.ports.drive_note_repository import DriveNoteRepository

_EXPECTED = {
    # reads by id and by key (§4.4, §5.1 a)
    "get", "get_many", "find_bound", "find_by_match_keys", "note_ids", "count",
    # history (§4.4 undo, §4.8)
    "get_version", "list_versions",
    # the owner-write transaction (§4.4 step 5)
    "write_version",
    # the tick (§5.1 b) and the deleted-files search (§6.4)
    "changed_since", "embedding_pending", "archived", "active", "find_nearest",
    # lifecycle, the parent in place (§7.1, §7.2, §7.5, §5.1 b step 5)
    "archive", "reactivate", "refresh_snapshot", "set_vector",
    # erase (§4.8)
    "erase", "delete_stub",
}


def test_abstract_methods_are_exactly_the_contract():
    assert set(DriveNoteRepository.__abstractmethods__) == _EXPECTED


def test_cannot_instantiate():
    with pytest.raises(TypeError):
        DriveNoteRepository()  # type: ignore[abstract]
