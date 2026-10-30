from datetime import datetime, timezone

from src.domain.drive_card import (
    card_eligible, card_hash, card_text, content_key_of, effective_date, photo_location,
)
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_media_facts import DriveMediaFacts
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
               media=DriveMediaFacts(taken_at=datetime(2025, 8, 14, 10, 0, tzinfo=timezone.utc), camera_make="Apple",
                                     camera_model="iPhone 15", image_width=4032, image_height=3024))
    text = card_text(rec)
    for part in ("taken 2025-08-14", "camera Apple iPhone 15", "place Rome (Roma), Italy", "4032x3024"):
        assert part in text


def test_video_card_has_duration():
    rec = _rec(name="Recording.webm", kind="video", media=DriveMediaFacts(
        video_duration_ms=93000, video_width=1280, video_height=720))
    assert "video 1:33" in card_text(rec)


def test_folder_and_opaque_cards():
    assert card_text(_rec(name="Contracts", kind="folder", is_folder=True, reason=Reason.FOLDER)) == "Contracts | folder"
    assert card_text(_rec(name="alek-site", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.OPAQUE_ROOT)) == "alek-site | repository"
    assert card_text(_rec(name=".obsidian", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.DOT_NAME)) == ".obsidian | hidden folder"
    assert card_text(_rec(name="mirror", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                          reason=Reason.DENSE_TREE, child_count=700)) == "mirror | generated files (500+ items)"


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
    assert card_eligible(_rec(card_capped=True)) is False


def test_effective_date_prefers_taken_then_fs_then_server_then_fallback():
    taken = DriveChange(item_id="i", media=DriveMediaFacts(taken_at=datetime(2025, 8, 14, 10, 0, tzinfo=timezone.utc)),
                        fs_modified_at=T0, modified_at=T0)
    assert effective_date(taken, T0).year == 2025
    assert effective_date(DriveChange(item_id="i", fs_modified_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                                      modified_at=T0), T0).year == 2024
    assert effective_date(DriveChange(item_id="i", modified_at=T0), T0) == T0
    assert effective_date(DriveChange(item_id="i", media=DriveMediaFacts()), T0) == T0   # S0e: empty photo facet


def test_content_key_is_the_rendered_content_identity():
    # MULTI_DRIVE_RFC §7.2, D12: the adapter builds the identity (OneDrive's quickXorHash -> family "quickxor"); the
    # key names its family, and an item without a provider hash has none
    assert content_key_of(ContentIdentity("quickxor", "abc=", 10)) == "quickxor:abc=:10"
    assert content_key_of(None) is None


def test_photo_location():
    assert photo_location(DriveMediaFacts(latitude=41.9, longitude=12.5)) == (41.9, 12.5)
    assert photo_location(DriveMediaFacts()) is None
