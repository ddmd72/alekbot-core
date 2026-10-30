"""The `card accuracy: N%` line (USER_DRIVE_INDEXING_RFC §26.4): on every rendered card that has a score, none on one
that has not, in the next line after `capture:`; `make_card` carries the record's score."""
from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope, TriageVerdict
from src.domain.drive_search import DriveSearchCard, _card_body, accuracy_line, make_card
from src.domain.drive_tier_render import card_body


def card(**kw):
    base = dict(ref="drive:x", item_id="x", name="scan.pdf", path="/Docs/scan.pdf", kind="pdf", is_folder=False,
                summary="scan.pdf | PDF document", state="indexed", nonce="abcdefgh")
    base.update(kw)
    return DriveSearchCard(**base)


def test_the_line_text():
    assert accuracy_line(card(card_accuracy=88)) == "card accuracy: 88%"
    assert accuracy_line(card(card_accuracy=0)) == "card accuracy: 0%"
    assert accuracy_line(card()) is None


def test_the_legacy_card_body_shows_it_after_the_capture_line():
    body = _card_body(card(card_accuracy=94, capture="scan", capture_source="detected"))
    lines = body.splitlines()
    assert lines[lines.index("capture: scan (detected)") + 1] == "card accuracy: 94%"


def test_the_tier_card_shows_it_after_the_capture_line():
    lines = card_body(card(card_accuracy=94, capture="scan", capture_source="detected")).splitlines()
    assert lines[lines.index("capture: scan (detected)") + 1] == "card accuracy: 94%"


def test_no_score_no_line():
    assert "card accuracy" not in _card_body(card())
    assert "card accuracy" not in card_body(card())


def test_make_card_carries_the_score():
    # a record that carries a score (§26.4 "What carries it"): readable, with a model profile
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="x", name="scan.pdf", kind="pdf", scope=Scope.NORMAL,
                           state=RecordState.INDEXED, indexed_generation=1, profile_source="model",
                           profile=DriveProfile(title="t", what_it_is="w", language="en", doc_type="other"),
                           card_accuracy=71)
    assert make_card(rec, "/Docs/scan.pdf", "s", "abcdefgh").card_accuracy == 71
    assert make_card(DriveIndexRecord(user_id="u", connection_id="u", item_id="x"), "/", "s", "abcdefgh").card_accuracy is None
    # a stored score on a record that no longer carries one (a photograph now) is never shown
    photo = DriveIndexRecord(user_id="u", connection_id="u", item_id="p", name="a.jpg", kind="image", state=RecordState.METADATA_ONLY,
                             triage_verdict=TriageVerdict.PHOTO, card_accuracy=71)
    assert make_card(photo, "/a.jpg", "s", "abcdefgh").card_accuracy is None
