"""Files without doc2query BY POLICY (books, sheets, data) are not a coverage gap (UAT 2026-10-14, the Barcelona query).

They were searched by their own words; only translated and synonym keywords are missing, by design (§25.3). Counting
them made every result read "Coverage: incomplete", so Smart hedged ("not a guarantee") and re-checked found files.
Now they never make the coverage line incomplete; a neutral note names them only when the result holds no exact and no
automatic match — the case where knowing what was not translated helps a not-found.
"""
from dataclasses import replace

from src.domain.drive_match import MatchTier
from src.domain.drive_search import render_results
from src.domain.drive_tier_result import CoverageFacts, TierBlock, TieredResult

from tests.unit.domain.test_drive_tier_render import example

NOTE = "2 books, sheets and data files were searched by their own words only, with no translated or synonym keywords."


def _with(coverage, blocks=None, keep=None):
    r = example()
    cards = r.cards if keep is None else tuple(c for c in r.cards if c.tier in keep)
    tiered = replace(r.tiered, coverage=coverage, blocks=r.tiered.blocks if blocks is None else blocks)
    return render_results(replace(r, cards=cards, tiered=tiered))


def test_policy_exclusions_never_make_coverage_incomplete():
    text = _with(CoverageFacts(no_passages=2), blocks=(TierBlock(MatchTier.ABOUT, 1),), keep=("about",))
    assert "Coverage: complete." in text
    assert "no passage keywords" not in text and NOTE not in text


def test_the_note_appears_only_when_nothing_exact_or_automatic_matched():
    text = _with(CoverageFacts(no_passages=2), blocks=(TierBlock(MatchTier.RELATED, 1),), keep=("related",))
    assert "Coverage: complete." in text
    assert NOTE in text


def test_the_note_is_absent_with_an_automatic_match():
    text = _with(CoverageFacts(no_passages=2), blocks=(TierBlock(MatchTier.LABELLED, 5),), keep=("labelled",))
    assert NOTE not in text


def test_real_gaps_still_make_coverage_incomplete():
    text = _with(CoverageFacts(no_passages=2, not_described=3))
    assert "Coverage: incomplete — 3 text files are" in text
    assert "no passage keywords" not in text


def test_one_file_is_singular():
    text = _with(CoverageFacts(no_passages=1), blocks=(), keep=())
    assert "1 book, sheet or data file was searched by its own words only" in text
