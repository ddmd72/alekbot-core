"""Triage sends the provider's 800 px rendition (owner decision after the live check, 2026-10-13: at 800 px the
captions were more precise than at 176 px)."""
from src.domain.drive_index_params import TRIAGE_THUMBNAIL_EDGE_PX


def test_triage_uses_the_large_rendition():
    assert TRIAGE_THUMBNAIL_EDGE_PX == 800
