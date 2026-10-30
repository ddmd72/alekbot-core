"""The capture on the card (USER_DRIVE_INDEXING_RFC §26.3, CARD_VERSION 4): a segment of the card text (not for
`digital`, whose word would only shift the embedding), a `capture: <capture> (<source>)` line on every rendered card
that has one, the match source naming it, and one doc type per card (Δ64: the brief's is never shown)."""
from datetime import date

from src.domain.drive_card import card_text
from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_index import CARD_VERSION, DocumentBrief, DriveIndexRecord, RecordState, Scope, TriageVerdict
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import about_keys_of, name_keys_v2, query_concepts
from src.domain.drive_search import DriveSearchCard, _card_body, make_card
from src.domain.drive_tier_explain import explain
from src.domain.drive_tier_render import card_body
from src.domain.drive_tiers import Allowance, FileKeys, classify

PROFILE = DriveProfile(title="Citizen card", what_it_is="A citizen card", language="pt", doc_type="id_document",
                       dates=(ProfileDate("valid_until", date(2031, 5, 7)),))


def rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="scan_0001.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED)
    base.update(kw)
    return DriveIndexRecord(**base)


def card(**kw) -> DriveSearchCard:
    base = dict(ref="drive:x", item_id="x", name="scan_0001.pdf", path="/Docs/scan_0001.pdf", kind="pdf",
                is_folder=False, summary="scan_0001.pdf | PDF document", state="indexed", nonce="abcdefgh")
    base.update(kw)
    return DriveSearchCard(**base)


class TestCardText:
    def test_card_version_is_4(self):
        assert CARD_VERSION == 4

    def test_a_profiled_scan_carries_the_segment_after_its_kind(self):
        assert card_text(rec(profile=PROFILE, capture="scan", capture_source="detected")) == (
            "scan_0001.pdf | PDF document | scan | id_document | A citizen card | Citizen card")

    def test_a_brief_only_screenshot(self):
        text = card_text(rec(name="IMG_2.png", kind="image", capture="screenshot", capture_source="automatic",
                             triage_verdict=TriageVerdict.TEXT, brief=DocumentBrief(description="a chat")))
        assert text == "IMG_2.png | image | screenshot | a chat"

    def test_a_document_photo_reads_in_words(self):
        assert "| photo of a document |" in card_text(rec(profile=PROFILE, capture="document_photo",
                                                          capture_source="detected"))

    def test_digital_and_no_capture_add_nothing(self):
        plain = card_text(rec(profile=PROFILE))
        assert card_text(rec(profile=PROFILE, capture="digital", capture_source="detected")) == plain
        assert "digital" not in plain

    def test_the_brief_doc_type_is_never_shown(self):
        # Δ64: the profile's doc type is the only one a card shows; a v1 brief may still hold one
        text = card_text(rec(brief=DocumentBrief(doc_type="certificate", description="a card")))
        assert "certificate" not in text and text == "scan_0001.pdf | PDF document | a card"


class TestRendered:
    def test_make_card_carries_the_capture(self):
        c = make_card(rec(profile=PROFILE, capture="scan", capture_source="detected"), "/Docs/scan_0001.pdf",
                      "summary", "abcdefgh", "a1")
        assert (c.capture, c.capture_source) == ("scan", "detected")

    def test_the_tier_card_shows_the_line_before_its_index_line(self):
        lines = card_body(card(capture="scan", capture_source="detected")).splitlines()
        assert "capture: scan (detected)" in lines
        assert lines.index("capture: scan (detected)") == lines.index("index: indexed") - 1

    def test_an_automatic_capture_says_so(self):
        assert "capture: screenshot (automatic)" in card_body(card(capture="screenshot", capture_source="automatic"))
        assert "capture: photo of a document (detected)" in card_body(card(capture="document_photo",
                                                                             capture_source="detected"))

    def test_digital_is_shown_too(self):
        assert "capture: digital (detected)" in card_body(card(capture="digital", capture_source="detected"))

    def test_no_capture_no_line(self):
        assert "capture:" not in card_body(card())

    def test_the_filter_listing_card_shows_it_too(self):
        assert "capture: scan (detected)" in _card_body(card(capture="scan", capture_source="detected"))


def _sources(record, query):
    concepts = query_concepts(query).concepts
    f = FileKeys("f", frozenset(about_keys_of(record)), frozenset(name_keys_v2("x.pdf")), {}, {})
    match = classify(f, concepts, Allowance(), {}, {})
    return explain(match, record, "x.pdf", None, ()).sources


class TestMatchSource:
    def test_a_detected_capture(self):
        assert _sources(RecordKeyInputs(kind="pdf", capture="scan", capture_source="detected"), "скани") == (
            'capture "скани"',)

    def test_an_automatic_capture(self):
        assert _sources(RecordKeyInputs(kind="pdf", capture="scan", capture_source="automatic"), "скани") == (
            'capture "скани" (automatic)',)
