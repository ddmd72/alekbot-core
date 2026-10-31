"""OneDrive adapter: identities, typed media facts and content identity are built from Graph's raw shape (§7.2)."""
from datetime import datetime, timezone

import pytest

from src.adapters.microsoft.onedrive_adapter import (
    content_identity_from_graph, media_facts_from_graph, onedrive_drive_identity, onedrive_item_identity,
    _to_change,
)
from src.domain.drive_content_identity import ContentIdentity
from src.domain.drive_media_facts import DriveMediaFacts


def test_identities_are_id_space_drive_item():
    assert onedrive_drive_identity("d1") == "microsoft_onedrive:d1"
    assert onedrive_item_identity("d1", "i1") == "microsoft_onedrive:d1:i1"
    assert onedrive_item_identity("d1", "i1", "fake_x") == "fake_x:d1:i1"


def test_an_unknown_part_gives_no_identity():
    assert onedrive_drive_identity("") == ""
    assert onedrive_item_identity("", "i1") == "" and onedrive_item_identity("d1", "") == ""


def test_an_empty_raw_item_has_neutral_media():
    assert media_facts_from_graph({}) == DriveMediaFacts()


def test_photo_is_typed_and_an_offsetless_time_is_utc():
    media = media_facts_from_graph({"photo": {"takenDateTime": "2025-05-01T10:00:00", "cameraMake": "Acme"}})
    assert media.taken_at == datetime(2025, 5, 1, 10, tzinfo=timezone.utc)
    assert (media.camera_make, media.camera_model) == ("Acme", "")


def test_an_empty_photo_facet_is_no_camera():
    media = media_facts_from_graph({"photo": {}})
    assert media.taken_at is None and media.camera_make == "" and media.camera_model == ""


@pytest.mark.parametrize("location", [{"latitude": 41.15}, {"latitude": "41.15", "longitude": "-8.61"},
                                      {"latitude": True, "longitude": -8.61}, "text"])
def test_a_location_needs_two_numbers(location):
    assert media_facts_from_graph({"location": location}).location is None


def test_a_location_is_a_float_pair():
    assert media_facts_from_graph({"location": {"latitude": 41, "longitude": -8}}).location == (41.0, -8.0)


def test_sizes_and_durations_read_as_non_negative_ints():
    media = media_facts_from_graph({"image": {"width": 4000.0, "height": -1}, "video": {"duration": "61000"},
                                    "audio": {"duration": 30000}})
    assert (media.image_width, media.image_height) == (4000, 0)
    assert media.video_duration_ms == 0 and media.audio_duration_ms == 30000


def test_package_and_malware_are_flags_by_presence():
    media = media_facts_from_graph({"package": {"type": "oneNote"}, "malware": {}})
    assert media.package and media.malware and not media.is_alias


def test_an_alias_names_the_item_it_points_to():
    raw = {"remoteItem": {"id": "r1", "parentReference": {"driveId": "d9"}}}
    assert media_facts_from_graph(raw, drive_id="d1").alias_of == "microsoft_onedrive:d9:r1"


def test_an_alias_without_a_drive_points_into_the_items_own_drive():
    assert media_facts_from_graph({"remoteItem": {"id": "r1"}}, drive_id="d1").alias_of == "microsoft_onedrive:d1:r1"


def test_an_alias_that_says_nothing_is_still_an_alias():
    media = media_facts_from_graph({"remoteItem": {}})
    assert media.alias_of == "" and media.is_alias


def test_content_identity_is_quickxor_digest_and_size():
    raw = {"size": 12, "file": {"hashes": {"quickXorHash": "abc="}}}
    assert content_identity_from_graph(raw) == ContentIdentity("quickxor", "abc=", 12)


@pytest.mark.parametrize("raw", [{"size": 1, "file": {}}, {"size": 1, "file": {"hashes": {"sha1Hash": "x"}}},
                                 {"size": 0, "folder": {}}, {"folder": {}, "file": {"hashes": {"quickXorHash": "a"}}}])
def test_no_hash_or_a_folder_has_no_content_identity(raw):
    assert content_identity_from_graph(raw) is None


def test_a_change_takes_its_drive_from_the_item_then_from_the_fallback():
    own = _to_change({"id": "i1", "name": "a", "file": {}, "parentReference": {"driveId": "d1"}}, "d2")
    assert own.identity == "microsoft_onedrive:d1:i1"
    fallback = _to_change({"id": "i1", "name": "a", "file": {}}, "d2")
    assert fallback.identity == "microsoft_onedrive:d2:i1"
    assert _to_change({"id": "i1", "name": "a", "file": {}}).identity == ""


def test_a_tombstone_keeps_its_identity_and_nothing_else():
    change = _to_change({"id": "i1", "deleted": {}, "name": "a", "size": 9}, "d2")
    assert change.deleted and change.identity == "microsoft_onedrive:d2:i1"
    assert change.size_bytes == 0 and change.media == DriveMediaFacts()


def test_a_folder_has_no_content_version():
    assert _to_change({"id": "f", "name": "x", "folder": {"childCount": 2}, "cTag": "c1"}).content_version == ""
