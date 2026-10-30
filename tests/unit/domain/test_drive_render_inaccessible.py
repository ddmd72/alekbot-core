"""Search results for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ50, Δ52, Δ56): the card keeps its
tier and its `index:` line says the file exists, the bot cannot open it, and a copy inside the bot's folder can be read;
a folder card gets the folder form; the coverage line counts such files on their own, never as "not read yet"."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_match import MatchTier
from src.domain.drive_search import (
    STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, make_card, render_results,
)
from src.domain.drive_search_texts import ACCESS_LINE_FILE, ACCESS_LINE_FOLDER, ACCESS_NOTE_FILE, ACCESS_NOTE_FOLDER
from src.domain.drive_tier_render import _coverage_line, coverage_parts
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult

AS_OF = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def rec(item="F1", name="card scan.pdf", folder=False, **kw):
    kw.setdefault("state", RecordState.METADATA_ONLY if folder else RecordState.PENDING)
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item, name=name, is_folder=folder, kind="folder" if folder else "pdf",
                            scope=Scope.NORMAL, has_card=True, **kw)


def tiered_result(*cards, coverage=CoverageFacts()):
    blocks = (TierBlock(MatchTier.ABOUT, len(cards)),)
    return DriveSearchResult(status=STATUS_OK, cards=tuple(replace(c, tier=MatchTier.ABOUT.value) for c in cards),
                             envelope=DriveSearchEnvelope(index_as_of=AS_OF),
                             request=DriveSearchRequest(query="card"),
                             tiered=TieredResult(concepts=("card",), coverage=coverage, blocks=blocks))


def test_the_texts_match_the_rfc():
    # Δ75: a new folder the user creates inside the bot's folder (a copy in the same folder can stay closed)
    assert ACCESS_LINE_FILE == ("index: the file exists, but the bot cannot open it (its sharing settings keep the "
                                "bot out); to fix it, copy the file into a new folder you create inside the bot's "
                                "folder: the copy can be read.")
    assert ACCESS_LINE_FOLDER == ("index: the folder exists, but the bot cannot open this folder or its files (their "
                                  "sharing settings keep the bot out); to fix it, copy the files into a new folder you "
                                  "create inside the bot's folder: the copies can be read.")
    assert "same folder" not in ACCESS_LINE_FILE + ACCESS_LINE_FOLDER
    assert ACCESS_NOTE_FILE == "The" + ACCESS_LINE_FILE[len("index: the"):]
    assert ACCESS_NOTE_FOLDER == "The" + ACCESS_LINE_FOLDER[len("index: the"):]


def test_make_card_carries_the_marking():
    card = make_card(rec(inaccessible_since=SINCE), "Docs/card scan.pdf", "card scan.pdf | PDF document", "n0000000", "a1")
    assert card.inaccessible and not card.stale
    open_card = make_card(rec(), "Docs/card scan.pdf", "card scan.pdf | PDF document", "n0000000", "a1")
    assert not open_card.inaccessible


def test_a_kept_readable_generation_is_shown_stale():
    kept = rec(inaccessible_since=SINCE, indexed_generation=2, indexed_version="c1", content_version="c1",
               state=RecordState.INDEXED)
    card = make_card(kept, "card scan.pdf", "s", "n0000000", "a1")
    assert card.stale and card.inaccessible


def test_a_tier_card_says_the_file_cannot_be_opened():
    card = make_card(rec(inaccessible_since=SINCE), "Docs/card scan.pdf", "card scan.pdf | PDF document", "n1111111", "a1")
    text = render_results(tiered_result(card))
    assert ACCESS_LINE_FILE in text and "index: pending" not in text


def test_a_folder_card_gets_the_folder_form():
    card = make_card(rec("D1", "Shared", folder=True, inaccessible_since=SINCE), "Shared", "Shared | folder",
                     "n2222222", "a1")
    text = render_results(tiered_result(card))
    assert ACCESS_LINE_FOLDER in text and ACCESS_LINE_FILE not in text


def test_a_related_card_carries_the_line_too():
    card = replace(make_card(rec(inaccessible_since=SINCE), "card scan.pdf", "s", "n3333333", "a1"),
                   tier=MatchTier.RELATED.value)
    result = DriveSearchResult(status=STATUS_OK, cards=(card,), envelope=DriveSearchEnvelope(index_as_of=AS_OF),
                               request=DriveSearchRequest(query="card"),
                               tiered=TieredResult(concepts=("card",), blocks=(TierBlock(MatchTier.RELATED, 1),)))
    assert ACCESS_LINE_FILE in render_results(result)


def test_the_coverage_line_counts_them_on_their_own():
    # closed files are a fact of the scope, not a gap: never a coverage part, their own sentence after the line
    assert coverage_parts(CoverageFacts(inaccessible=3, not_read_yet=2), passages_tiers_empty=True) == [
        "2 documents are not read yet"]
    assert coverage_parts(CoverageFacts(inaccessible=1), passages_tiers_empty=True) == []
    one = _coverage_line(TieredResult(concepts=("card",), coverage=CoverageFacts(inaccessible=1)))
    assert one == "Coverage: complete. 1 file cannot be opened by the bot."
    many = _coverage_line(TieredResult(concepts=("card",), coverage=CoverageFacts(inaccessible=3, not_read_yet=2)))
    assert many == "Coverage: incomplete — 2 documents are not read yet. 3 files cannot be opened by the bot."
    assert "incomplete" not in one


def test_a_filter_listing_card_and_envelope():
    card = make_card(rec(inaccessible_since=SINCE), "card scan.pdf", "s", "n4444444", "a1")
    result = DriveSearchResult(status=STATUS_OK, cards=(card,),
                               envelope=DriveSearchEnvelope(index_as_of=AS_OF, inaccessible=2),
                               request=DriveSearchRequest(query="", kind="pdf"), matched_total=1)
    text = render_results(result)
    assert ACCESS_LINE_FILE in text
    assert "cannot be opened by the bot" not in text.replace(ACCESS_LINE_FILE, "")     # Δ93: omitted with results
    empty = DriveSearchResult(status=STATUS_OK, cards=(), envelope=DriveSearchEnvelope(index_as_of=AS_OF, inaccessible=2),
                              request=DriveSearchRequest(query="", kind="pdf"), matched_total=0)
    assert "2 files cannot be opened by the bot (their sharing settings keep it out)" in render_results(empty)


def test_an_accessible_card_is_unchanged():
    card = DriveSearchCard(ref="drive:A", item_id="A", name="a.pdf", path="a.pdf", kind="pdf", is_folder=False,
                           summary="a.pdf", state="indexed", nonce="n5555555")
    text = render_results(tiered_result(card))
    assert "index: indexed" in text and "cannot open" not in text
