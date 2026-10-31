"""The mirror's path limit is the connection's `max_path_chars`, passed to `fits`, `place` and `archive_place`; the
default stays OneDrive's."""
from src.domain.drive_index_params import MIRROR_PATH_MARGIN
from src.domain.drive_mirror_names import archive_place, fits, place


def test_fits_follows_the_given_limit():
    parts = ["x" * 90]
    assert fits("", "", parts, 90 + MIRROR_PATH_MARGIN)
    assert not fits("", "", parts, 90 + MIRROR_PATH_MARGIN - 1)


def test_place_goes_to_long_paths_under_a_short_limit_only():
    parts = ("folder", "x" * 60)
    assert not place(parts, area_path="", root_path="", max_path=1000).long_path
    assert place(parts, area_path="", root_path="", max_path=50).long_path


def test_archive_place_follows_the_given_limit():
    parts = ("folder", "x" * 60)
    assert not archive_place("2026-10-30", parts, area_path="", root_path="", max_path=1000).long_path
    assert archive_place("2026-10-30", parts, area_path="", root_path="", max_path=50).long_path
