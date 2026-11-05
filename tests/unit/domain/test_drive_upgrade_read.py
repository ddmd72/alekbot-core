"""U5 Part D: `upgrade_reread` is true only for a record stamped by a stale `parser@N` link whose content was not
edited since (indexed: `indexed_version == content_version`; otherwise `dirty_since` unset)."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_index import DriveIndexRecord, Reason, RecordState
from src.domain.drive_upgrade_read import upgrade_reread

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)
VERSIONS = {"parser": 4, "reader": 1}


def indexed_rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u1", connection_id="u1", item_id="i1", kind="pdf", state=RecordState.INDEXED,
                content_version="v7", indexed_version="v7", indexed_generation=4, extracted_by="parser@3")
    base.update(kw)
    return DriveIndexRecord(**base)


def test_a_stale_parser_stamp_on_unedited_content_is_an_upgrade_re_read():
    assert upgrade_reread(indexed_rec(), VERSIONS)


def test_an_edited_indexed_record_is_not():
    assert not upgrade_reread(indexed_rec(content_version="v8"), VERSIONS)


def test_a_demoted_record_is_an_upgrade_unless_an_edit_made_it_dirty():
    demoted = indexed_rec(state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT, indexed_generation=None,
                          indexed_version=None)
    assert upgrade_reread(demoted, VERSIONS)
    assert not upgrade_reread(replace(demoted, dirty_since=NOW), VERSIONS)


def test_a_failed_record_follows_the_same_rule():
    failed = indexed_rec(state=RecordState.FAILED, reason=Reason.EXTRACT_ERROR, indexed_generation=None,
                         indexed_version=None)
    assert upgrade_reread(failed, VERSIONS)
    assert not upgrade_reread(replace(failed, dirty_since=NOW), VERSIONS)


def test_reader_transcript_current_and_unstamped_records_are_never_upgrades():
    assert not upgrade_reread(indexed_rec(extracted_by="reader@0"), VERSIONS)
    assert not upgrade_reread(indexed_rec(extracted_by="reader@1"), VERSIONS)
    assert not upgrade_reread(indexed_rec(extracted_by="transcribe-gemini@1", kind="audio"), VERSIONS)
    assert not upgrade_reread(indexed_rec(extracted_by="parser@4"), VERSIONS)
    assert not upgrade_reread(indexed_rec(extracted_by=None), VERSIONS)
