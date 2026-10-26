"""What Smart reads after the Search v2 cross-cutting review (USER_DRIVE_INDEXING_RFC §25.5.6, §25.8): negatives over
an incomplete coverage line speak of the files searched (xc F4), a validity date on the card (F5), a state not rebuilt
under v2 states nothing (XC-1), and a filter-only listing names the words it did not search for (F2)."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_match import MatchTier
from src.domain.drive_search import (
    STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, render_results,
)
from src.domain.drive_search_texts import REBUILDING_LINE
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult

AS_OF = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def _card(item="A1", tier=MatchTier.ABOUT, **kw) -> DriveSearchCard:
    return DriveSearchCard(ref=f"drive:{item}", item_id=item, name="a.pdf", path="a.pdf", kind="pdf", is_folder=False,
                           summary="a.pdf | PDF document", state="indexed", nonce=f"n{item}00000", tier=tier.value,
                           **kw)


def _result(tiered: TieredResult, cards=()) -> DriveSearchResult:
    return DriveSearchResult(status=STATUS_OK, cards=tuple(cards), envelope=DriveSearchEnvelope(index_as_of=AS_OF),
                             request=DriveSearchRequest(query="x"), tiered=tiered)


INCOMPLETE = [CoverageFacts(building=True), CoverageFacts(not_described=2), CoverageFacts(not_read_yet=1),
              CoverageFacts(unread=1), CoverageFacts(rekeying=True), CoverageFacts(reading=True)]


# -- F4 --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("coverage", INCOMPLETE)
def test_an_absent_word_under_incomplete_coverage_is_held_by_no_searched_file(coverage):
    text = render_results(_result(TieredResult(concepts=("налоговая",), coverage=coverage, absent=("налоговая",))))
    assert 'No searched file holds "налоговая".' in text and 'No file holds "налоговая".' not in text


def test_an_absent_word_under_complete_coverage_keeps_the_categorical_line():
    text = render_results(_result(TieredResult(concepts=("dog",), absent=("dog",))))
    assert 'No file holds "dog".' in text and "searched file" not in text


@pytest.mark.parametrize("coverage", INCOMPLETE)
def test_absent_in_scope_under_incomplete_coverage(coverage):
    text = render_results(_result(TieredResult(concepts=("dog",), coverage=coverage, absent_in_scope=("dog",))))
    assert 'No searched file in this scope holds "dog".' in text


@pytest.mark.parametrize("coverage", INCOMPLETE)
def test_the_exact_footer_under_incomplete_coverage(coverage):
    tiered = TieredResult(concepts=("rome",), coverage=coverage, blocks=(TierBlock(MatchTier.ABOUT, 1),))
    text = render_results(_result(tiered, [_card()]))
    assert text.endswith("No other searched file is about or mentions all of these words.")


def test_the_exact_footer_under_complete_coverage():
    tiered = TieredResult(concepts=("rome",), blocks=(TierBlock(MatchTier.ABOUT, 1),))
    assert render_results(_result(tiered, [_card()])).endswith(
        "No other file is about or mentions all of these words.")


def test_the_empty_conjunction_under_incomplete_coverage():
    tiered = TieredResult(concepts=("a1", "b1"), coverage=CoverageFacts(building=True),
                          conjunction=(("coimbra", 41), ("trip", 2)))
    text = render_results(_result(tiered))
    assert 'No searched file matches all of: "coimbra" (41 files), "trip" (2 files).' in text


# -- F5 --------------------------------------------------------------------------------------------------------------

def test_a_card_with_a_validity_date_shows_it():
    card = _card(effective_date=datetime(2016, 12, 9, tzinfo=timezone.utc), date_source="file date",
                 valid_until=datetime(2026, 12, 9, tzinfo=timezone.utc))
    text = render_results(_result(TieredResult(concepts=("passport",), blocks=(TierBlock(MatchTier.ABOUT, 1),)),
                                  [card]))
    assert "date: 2016-12-09 (file date)\nvalid until: 2026-12-09" in text


def test_a_validity_match_dates_the_card_by_its_expiry():
    card = _card(tier=MatchTier.LABELLED, effective_date=datetime(2016, 12, 9, tzinfo=timezone.utc),
                 date_source="file date", valid_until=datetime(2026, 12, 9, tzinfo=timezone.utc), expiry_match=True)
    text = render_results(_result(TieredResult(concepts=("истекает",), blocks=(TierBlock(MatchTier.LABELLED, 1),)),
                                  [card]))
    assert "date: 2026-12-09 (expires)" in text and "2016-12-09" not in text and "valid until" not in text


def test_a_card_without_a_validity_date_has_no_line():
    text = render_results(_result(TieredResult(concepts=("rome",), blocks=(TierBlock(MatchTier.ABOUT, 1),)),
                                  [_card()]))
    assert "valid until" not in text


def test_a_filter_listing_card_shows_its_validity_date():
    card = DriveSearchCard(ref="drive:A", item_id="A", name="p.pdf", path="p.pdf", kind="pdf", is_folder=False,
                           summary="p.pdf | PDF document", state="indexed", nonce="nA0000000",
                           valid_until=datetime(2027, 1, 2, tzinfo=timezone.utc))
    result = DriveSearchResult(status=STATUS_OK, cards=(card,), request=DriveSearchRequest(kind="pdf"),
                               envelope=DriveSearchEnvelope(index_as_of=AS_OF, content_search=True),
                               matched_total=1)
    assert "valid until: 2027-01-02" in render_results(result)


# -- XC-1: a state not rebuilt under v2 ------------------------------------------------------------------------------

@pytest.mark.parametrize("request_", [DriveSearchRequest(query="rome tickets"), DriveSearchRequest(kind="pdf")])
def test_an_index_awaiting_its_rebuild_states_nothing(request_):
    result = DriveSearchResult(status=STATUS_OK, request=request_, awaiting_rebuild=True,
                               envelope=DriveSearchEnvelope(building=True, index_as_of=AS_OF),
                               matched_total=0 if request_.filter_only else None)
    text = render_results(result)
    assert text.endswith(REBUILDING_LINE)
    for claim in ("No file", "No matching", "match the filters", "newest"):
        assert claim not in text


# -- F2: the words a filter-only request did not search for --------------------------------------------------------

def test_a_listing_with_filters_names_the_words_it_did_not_search_for():
    result = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="all my", kind="pdf"),
                               envelope=DriveSearchEnvelope(index_as_of=AS_OF, content_search=True), matched_total=0)
    assert '0 files and folders match the filters; showing the newest 0. Not searched for: "all", "my".' in \
        render_results(result)


def test_a_listing_never_says_no_words_were_given_when_words_were_given():
    result = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(query="a"),
                               envelope=DriveSearchEnvelope(index_as_of=AS_OF, content_search=True), matched_total=0)
    text = render_results(result)
    assert "No name words or filters were given" not in text and 'No word of the request could be searched ("a")' \
        in text


def test_a_listing_without_words_or_filters_says_so():
    result = DriveSearchResult(status=STATUS_OK, request=DriveSearchRequest(),
                               envelope=DriveSearchEnvelope(index_as_of=AS_OF, content_search=True), matched_total=0)
    assert "No name words or filters were given" in render_results(result)
