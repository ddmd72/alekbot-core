"""`unreadable_reason`: why a record could not be read, as the Cabinet's list names it (USER_DRIVE_INDEXING_RFC §6)."""
import pytest

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_index_params import CABINET_UNREADABLE_MAX
from src.domain.drive_unreadable_file import UnreadableFile, unreadable_reason


def rec(**kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="a", kind="pdf", **kw)


@pytest.mark.parametrize("reason", [Reason.TOO_LARGE, Reason.NO_TEXT, Reason.MACHINE_TEXT, Reason.USER_CHUNK_CAP])
def test_a_metadata_only_record_gives_its_reason(reason):
    assert unreadable_reason(rec(state=RecordState.METADATA_ONLY, reason=reason)) == reason.value


def test_a_failed_record_is_failed():
    assert unreadable_reason(rec(state=RecordState.FAILED)) == "failed"


def test_a_failed_record_is_failed_whatever_its_reason():
    assert unreadable_reason(rec(state=RecordState.FAILED, reason=Reason.TOO_LARGE)) == "failed"


def test_the_file_is_a_frozen_value_and_the_list_is_short():
    f = UnreadableFile(name="scan_0012.pdf", reason="no_text", web_url="https://example.test/f")
    with pytest.raises(Exception):
        f.name = "x"  # type: ignore[misc]
    assert 0 < CABINET_UNREADABLE_MAX <= 10
