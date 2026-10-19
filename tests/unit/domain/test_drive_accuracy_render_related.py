"""A Related card shows `card accuracy: N%` too (USER_DRIVE_INDEXING_RFC §26.4, D36): it is opened, and answers a value
question, like any other card; one with no score shows no line."""
from src.domain.drive_search import DriveSearchCard
from src.domain.drive_tier_render import _related_body


def card(**kw):
    base = dict(ref="drive:x", item_id="x", name="scan.pdf", path="/Docs/scan.pdf", kind="pdf", is_folder=False,
                summary="scan.pdf | PDF document", state="indexed", nonce="abcdefgh")
    base.update(kw)
    return DriveSearchCard(**base)


def test_a_related_card_shows_its_score():
    assert "card accuracy: 72%" in _related_body(card(card_accuracy=72)).splitlines()


def test_a_related_card_without_a_score_shows_no_line():
    assert "card accuracy" not in _related_body(card())
