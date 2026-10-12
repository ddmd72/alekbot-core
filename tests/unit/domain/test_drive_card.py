from datetime import datetime, timezone

from src.domain.drive_card import (
    card_eligible, card_hash, card_text, content_hash_of, effective_date, photo_location,
)
from src.domain.drive_index import DriveChange, DriveIndexRecord, Reason, RecordState, Scope

T0 = datetime(2026, 10, 11, tzinfo=timezone.utc)


def _rec(**kw):
    base = dict(user_id="u", item_id="i", name="Договор_ред3.docx", kind="docx", scope=Scope.NORMAL,
                state=RecordState.METADATA_ONLY, reason=Reason.KIND_NOT_ADMITTED)
    base.update(kw)
    return DriveIndexRecord(**base)


def test_file_card_has_name_and_kind_and_no_path():
    text = card_text(_rec(parent_id="p"))
    assert text.startswith("Договор_ред3.docx | Word document")
    assert "/" not in text


def test_photo_card_has_taken_camera_place_dimensions():
    rec = _rec(name="IMG_1.HEIC", kind="image", place_name="Rome (Roma), Italy",
               facets={"photo": {"takenDateTime": "2025-08-14T10:00:00Z", "cameraMake": "Apple",
                                 "cameraModel": "iPhone 15"}, "image": {"width": 4032, "height": 3024}})
    text = card_text(rec)
    for part in ("taken 2025-08-14", "camera Apple iPhone 15", "place Rome (Roma), Italy", "4032x3024"):
        assert part in text


def test_video_card_has_duration():
    rec = _rec(name="Recording.webm", kind="video", facets={"video": {"duration": 93000, "width": 1280, "height": 720}})
    assert "video 1:33" in card_text(rec)


def test_folder_and_opaque_cards():
    assert card_text(_rec(name="Contracts", kind="folder", is_folder=True, reason=Reason.FOLDER)) == "Contracts | folder"
    assert card_text(_rec(name="alek-site", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.OPAQUE_ROOT)) == "alek-site | repository"
    assert card_text(_rec(name=".obsidian", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.DOT_NAME)) == ".obsidian | hidden folder"
    assert card_text(_rec(name="mirror", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.DENSE_TREE, child_count=700)) == "mirror | generated files (700 items)"


def test_hash_changes_with_text_only():
    a, b = _rec(), _rec(size_bytes=99, modified_at=T0)
    assert card_hash(a) == card_hash(b)
    assert card_hash(a) != card_hash(_rec(name="other.docx"))
    assert len(card_hash(a)) == 32


def test_eligibility():
    assert card_eligible(_rec()) is True
    assert card_eligible(_rec(scope=Scope.INSIDE_OPAQUE)) is False
    assert card_eligible(_rec(scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED)) is False
    assert card_eligible(_rec(state=RecordState.DELETING)) is False
    assert card_eligible(_rec(reason=Reason.USER_CARD_CAP)) is False


def test_effective_date_prefers_taken_then_fs_then_server_then_fallback():
    taken = DriveChange(item_id="i", facets={"photo": {"takenDateTime": "2025-08-14T10:00:00Z"}},
                        fs_modified_at=T0, modified_at=T0)
    assert effective_date(taken, T0).year == 2025
    assert effective_date(DriveChange(item_id="i", fs_modified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                                      modified_at=T0), T0).year == 2024
    assert effective_date(DriveChange(item_id="i", modified_at=T0), T0) == T0
    assert effective_date(DriveChange(item_id="i", facets={"photo": {}}), T0) == T0   # S0e: empty photo facet


def test_content_hash_needs_quickxor():
    ch = DriveChange(item_id="i", size_bytes=10, facets={"hashes": {"quickXorHash": "abc="}})
    assert content_hash_of(ch) == "abc=:10"
    assert content_hash_of(DriveChange(item_id="i", facets={"hashes": {"sha256Hash": "x"}})) is None


def test_photo_location():
    assert photo_location({"location": {"latitude": 41.9, "longitude": 12.5}}) == (41.9, 12.5)
    assert photo_location({"location": {}}) is None and photo_location({}) is None
