"""Fields added in fix round 3 survive the Firestore round trip (USER_DRIVE_INDEXING_RFC §3.2, §3.5)."""
from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord


def test_the_refused_chunk_space_round_trips():   # R3 F2-3
    rec = DriveIndexRecord(user_id="u", item_id="i", chunks_refused_space="gemini-embedding-2/768")
    doc = record_to_doc(rec)
    assert "chunks_refused_space" in RECORD_FIELDS and doc["chunks_refused_space"] == "gemini-embedding-2/768"
    assert record_from_doc(doc) == rec
