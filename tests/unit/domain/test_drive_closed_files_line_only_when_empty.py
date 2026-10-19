"""The closed-files sentence rides the coverage line (and a filter listing's envelope) only when the search found
nothing (USER_DRIVE_INDEXING_RFC §26.10, Δ93): with results it is noise, a matching closed file has its own access line."""
from dataclasses import replace
from datetime import datetime, timezone

from src.domain.drive_match import MatchTier
from src.domain.drive_search import (
    STATUS_OK, DriveSearchCard, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult, render_results,
)
from src.domain.drive_tier_render import _coverage_line
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult

AS_OF = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
SENTENCE = "cannot be opened by the bot"


def tiered(blocks=(), closed=1):
    return TieredResult(concepts=("card",), coverage=CoverageFacts(inaccessible=closed), blocks=tuple(blocks))


def test_results_present_omit_the_sentence():
    line = _coverage_line(tiered([TierBlock(MatchTier.ABOUT, 2)]))
    assert line == "Coverage: complete."
    assert SENTENCE not in line


def test_related_only_results_count_as_found():
    assert SENTENCE not in _coverage_line(tiered([TierBlock(MatchTier.RELATED, 1)]))


def test_nothing_found_singular():
    assert _coverage_line(tiered()).endswith(f"1 file {SENTENCE}.")


def test_nothing_found_plural():
    assert _coverage_line(tiered(closed=3)).endswith(f"3 files {SENTENCE}.")


def test_folder_scoped_follows_the_same_rule():
    # the folder-scoped count reaches the renderer as the same field
    assert SENTENCE not in _coverage_line(tiered([TierBlock(MatchTier.MENTIONED, 1)], closed=2))
    assert _coverage_line(tiered(closed=2)).endswith(f"2 files {SENTENCE}.")


def _listing(cards):
    return DriveSearchResult(status=STATUS_OK, cards=cards, request=DriveSearchRequest(query="", kind="pdf"),
                             envelope=DriveSearchEnvelope(index_as_of=AS_OF, inaccessible=2), matched_total=len(cards))


def test_listing_envelope_with_and_without_results():
    card = DriveSearchCard(item_id="A", is_folder=False, ref="drive:A", name="a.pdf", path="a.pdf", kind="pdf", summary="s", nonce="nonce0001")
    assert SENTENCE in render_results(_listing(()))
    assert SENTENCE not in render_results(_listing((replace(card),)))
