"""The Firestore record codec reads a stored null content identity (MULTI_DRIVE_RFC §7.2, §8, I7) as None. The slice-1b
fields' round trip and their defaults are in the every-field case (test_firestore_drive_index_repo_serialization.py)."""
from src.adapters.firestore_drive_index_repo import record_from_doc


def test_a_stored_null_content_identity_reads_none():
    assert record_from_doc({"user_id": "usr-1", "item_id": "item-1", "content_identity": None}).content_identity is None
