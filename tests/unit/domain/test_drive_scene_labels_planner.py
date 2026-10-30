"""Scene labels through the planner (D40): `triaged` stores a photograph's labels beside its other photo fields, and a
text verdict (or a photograph without a description) clears them."""
from datetime import datetime, timezone

from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_extraction import TriageResult
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope, TriageVerdict
from src.domain.drive_index_planner import triaged

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def _rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="a.jpg", kind="image", scope=Scope.NORMAL, state=RecordState.PENDING,
                content_version="c1")
    base.update(kw)
    return DriveIndexRecord(**base)


def _result(verdict):
    return TriageResult(item_id="x", verdict=verdict)


def test_a_photograph_keeps_its_labels():
    photo = PhotoDescription(description="d", scene="s", scene_labels=("sunset", "beach"))
    out = triaged(_rec(), _result(TriageVerdict.PHOTO), NOW, photo=photo)
    assert out.photo_scene_labels == ("sunset", "beach")


def test_a_photograph_without_a_description_has_none():
    out = triaged(_rec(photo_scene_labels=("sea",)), _result(TriageVerdict.PHOTO), NOW, photo=None)
    assert out.photo_scene_labels == ()


def test_the_coarse_location_is_rounded_to_three_decimals_and_absent_without_one():
    from src.domain.drive_card import coarse_location
    from src.domain.drive_media_facts import DriveMediaFacts

    assert coarse_location(DriveMediaFacts(latitude=41.40338291, longitude=2.17402516)) == (41.403, 2.174)
    assert coarse_location(DriveMediaFacts()) is None
