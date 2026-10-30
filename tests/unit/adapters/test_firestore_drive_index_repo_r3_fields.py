"""Fields added in fix round 3 survive the Firestore round trip (USER_DRIVE_INDEXING_RFC §3.2, §3.5)."""
from src.adapters.firestore_drive_index_repo import RECORD_FIELDS, record_from_doc, record_to_doc
from src.domain.drive_index import DriveIndexRecord


def test_the_refused_chunk_space_round_trips():   # R3 F2-3
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", chunks_refused_space="gemini-embedding-2/768")
    doc = record_to_doc(rec)
    assert "chunks_refused_space" in RECORD_FIELDS and doc["chunks_refused_space"] == "gemini-embedding-2/768"
    assert record_from_doc(doc) == rec


def test_the_parked_upgrade_month_round_trips():   # R3 F1-5
    from src.adapters.firestore_drive_index_repo import STATE_FIELDS, state_from_doc, state_to_doc
    from src.domain.drive_index import DriveSyncState
    state = DriveSyncState(user_id="u", connection_id="u", upgrade_parked_month="2026-10")
    doc = state_to_doc(state)
    assert "upgrade_parked_month" in STATE_FIELDS and doc["upgrade_parked_month"] == "2026-10"
    assert state_from_doc("u", doc) == state
