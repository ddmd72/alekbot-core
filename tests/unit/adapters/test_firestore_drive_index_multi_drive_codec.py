"""The Firestore record and sync-state codec reads the slice-1b fields back as their types (MULTI_DRIVE_RFC §7.2, §8,
I7): typed media facts and content identity as maps, the reattachment stamp as a plain UTC datetime, and a document
written before the fields existed reads with their neutral defaults."""
from datetime import datetime, timezone

from src.adapters.firestore_drive_index_repo import record_from_doc, record_to_doc, state_from_doc
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_index import DriveIndexRecord
from src.domain.drive_media_facts import DriveMediaFacts

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def test_a_record_with_the_new_fields_round_trips():
    rec = DriveIndexRecord(user_id="usr-1", item_id="item-1", connection_id="conn-1", identity="fake_a:drive-1:item-1",
                           content_identity=ContentIdentity("fam1", "d1", 10),
                           media=DriveMediaFacts(taken_at=NOW, latitude=1.0, longitude=2.0, alias_of=""))
    doc = record_to_doc(rec)
    assert doc["content_identity"] == {"family": "fam1", "digest": "d1", "size": 10}
    assert doc["media"]["taken_at"] == NOW and doc["media"]["alias_of"] == ""
    assert doc["connection_id"] == "conn-1" and doc["identity"] == "fake_a:drive-1:item-1"
    assert record_from_doc(doc) == rec


def test_a_record_written_before_the_fields_reads_with_their_defaults():
    rec = record_from_doc({"user_id": "usr-1", "item_id": "item-1"})
    assert (rec.connection_id, rec.identity, rec.content_identity, rec.media) == ("", "", None, DriveMediaFacts())


def test_a_stored_null_content_identity_reads_none():
    assert record_from_doc({"user_id": "usr-1", "item_id": "item-1", "content_identity": None}).content_identity is None


def test_the_sync_state_fields_read_back():
    state = state_from_doc("usr-1", {"connection_id": "conn-1", "drive_identity": "fake_a:drive-1"})
    assert (state.connection_id, state.drive_identity) == ("conn-1", "fake_a:drive-1")
