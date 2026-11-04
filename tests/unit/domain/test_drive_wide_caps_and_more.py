"""DRIVE_MCP_CONNECTOR_RFC §14.2, §14.4, E22: the caps values (one per surface, the chat's by default), the passages
selection and merge under `wide`, `more` (a ranked chunk left, or one the merge dropped), the search's "fewer shown
than matched", and the parsing of `wide` and `batch`. Pure. Invented items: Tom's plan (Acme Hamburg), the motor policy
MP-2026-118204, the garden-shed planning permission, the Hamburg conference."""
from dataclasses import replace

import pytest

from src.domain.drive_excerpt_caps import ExcerptCaps
from src.domain.drive_excerpt_chunk import ExcerptChunk
from src.domain.drive_excerpt_file import ExcerptFile
from src.domain.drive_excerpt_request import ExcerptRequest
from src.domain.drive_excerpt_result import ExcerptResult
from src.domain.drive_excerpts import select_excerpts
from src.domain.drive_index_params import (
    ABOUT_SHOWN, EXCERPTS_CHARS_MAX, EXCERPTS_K, EXCERPTS_PER_FILE_MAX, LABELLED_SHOWN, MENTIONED_SHOWN,
    RESULT_CHARS_MAX, TRANSLATED_SHOWN,
)
from src.domain.drive_match import MatchTier
from src.domain.drive_search import STATUS_DISABLED, STATUS_OK, rendered_results, render_results
from src.domain.drive_search_caps import SearchCaps
from src.domain.drive_search_merge import merge_excerpts
from src.domain.drive_tier_render import render_tiered, tiered_text
from src.domain.drive_tier_result import GroupMember, TierBlock
from src.domain.drive_wide_batch import BAD_BATCH, parse_batch, parse_wide
from tests.unit.domain.test_drive_search_merge import ACME, DRIVE
from tests.unit.domain.test_drive_tier_render import card, example


def _ids(item_id, n, generation=1):
    return [(item_id, generation, i) for i in range(n)]


def _sizes(ids, size=500):
    return {cid: size for cid in ids}


# -- the caps values ------------------------------------------------------------------------------------------------

def test_the_default_caps_are_the_chats_and_wide_doubles_passages_and_characters_but_not_per_file():
    assert ExcerptCaps.DEFAULT == ExcerptCaps(k=EXCERPTS_K, chars=EXCERPTS_CHARS_MAX, per_file=EXCERPTS_PER_FILE_MAX)
    assert (ExcerptCaps.WIDE.k, ExcerptCaps.WIDE.chars, ExcerptCaps.WIDE.per_file) == (20, 45_000, 8)


def test_the_search_caps_per_tier_and_the_result_characters():
    assert SearchCaps.DEFAULT == SearchCaps(about=ABOUT_SHOWN, mentioned=MENTIONED_SHOWN, translated=TRANSLATED_SHOWN,
                                            labelled=LABELLED_SHOWN, result_chars=RESULT_CHARS_MAX)
    wide = SearchCaps.WIDE
    assert [wide.shown(t) for t in (MatchTier.ABOUT, MatchTier.MENTIONED, MatchTier.TRANSLATED,
                                     MatchTier.LABELLED)] == [20, 12, 12, 16]
    assert wide.result_chars == 40_000
    with pytest.raises(KeyError):
        wide.shown(MatchTier.RELATED)   # Related keeps its own cap (RELATED_MAX) in both columns


# -- selection under the caps ---------------------------------------------------------------------------------------

def test_the_default_selection_is_unchanged_by_the_caps_argument():
    ranked = _ids("plan", 5) + _ids("policy", 5) + _ids("shed", 5)
    assert select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes={}) == select_excerpts(
        ranked, _sizes(ranked), shown=frozenset(), hashes={}, caps=ExcerptCaps.DEFAULT)


def test_wide_selects_twice_as_many_chunks_with_the_same_per_file_cap():
    ranked = _ids("plan", 12) + _ids("policy", 6) + _ids("shed", 6) + _ids("conference", 6)
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset(), hashes={}, caps=ExcerptCaps.WIDE)
    assert len(sel.chunks) == 20
    assert [c for c in sel.chunks if c[0] == "plan"] == _ids("plan", EXCERPTS_PER_FILE_MAX)
    assert sel.left   # the plan's chunks past the per-file cap and the conference's past k


def test_wide_takes_characters_up_to_its_own_cap():
    ranked = _ids("policy", 1) + _ids("shed", 1) + _ids("conference", 1)
    sizes = {ranked[0]: 20_000, ranked[1]: 20_000, ranked[2]: 3_000}
    assert select_excerpts(ranked, sizes, shown=frozenset(), hashes={}).chunks == (ranked[0], ranked[2])
    assert select_excerpts(ranked, sizes, shown=frozenset(), hashes={}, caps=ExcerptCaps.WIDE).chunks == tuple(ranked)


@pytest.mark.parametrize("ranked, shown, left", [
    (_ids("plan", 3), frozenset(), False),                                  # everything chosen
    (_ids("plan", 3), frozenset(_ids("plan", 1)), False),                   # the rest shown earlier or chosen now
    (_ids("plan", 12), frozenset(), True),                                  # past the per-file cap
    (_ids("plan", 6) + _ids("policy", 6), frozenset(), True),               # past k
    (_ids("plan", 6) + _ids("policy", 6), frozenset(_ids("policy", 2)), False),   # the two left were shown before
])
def test_left_is_a_ranked_chunk_neither_shown_nor_chosen(ranked, shown, left):
    assert select_excerpts(ranked, _sizes(ranked), shown=shown, hashes={}).left is left


def test_left_maps_a_copy_onto_its_keeper():
    # the shed permission's scan is a byte-identical copy of the original: its chunks are the original's
    ranked = [("shed", 1, 0), ("shed-copy", 4, 0), ("shed-copy", 4, 1)]
    hashes = {"shed": "sha:shed", "shed-copy": "sha:shed"}
    sel = select_excerpts(ranked, _sizes(ranked), shown=frozenset({("shed", 1, 1)}), hashes=hashes)
    assert sel.chunks == (("shed", 1, 0),) and sel.left is False


def test_left_holds_for_a_chunk_skipped_by_the_character_cap():
    ranked = _ids("policy", 2)
    sizes = {ranked[0]: EXCERPTS_CHARS_MAX, ranked[1]: 10}
    sel = select_excerpts(ranked, sizes, shown=frozenset(), hashes={})
    assert sel.chunks == (ranked[0],) and sel.left is True


# -- the merge under the caps, and its `more` -----------------------------------------------------------------------

def _chunk(item, index, text="x"):
    return ExcerptChunk(item, 1, index, text, 0, len(text))


def _file(tag, item, path, indexes):
    return ExcerptFile(item_id=item, ref=f"drive:{tag}:{item}", name=path, path=path, kind="docx", generation=1,
                       chunk_count=20, ref_tag=tag, chunks=tuple(_chunk(item, i) for i in indexes))


REQUEST = ExcerptRequest(question="What does Tom's plan say happens after the first review?")


def _result(*files, **kw):
    return ExcerptResult(status=STATUS_OK, request=REQUEST, files=tuple(files), **kw)


def _merge(parts, **kw):
    return merge_excerpts(parts, request=REQUEST, not_searched=(), no_text_listed=10, **kw)


def _two_drives():
    drive = (DRIVE, _result(*(_file("a1", f"d{i}", f"Drive/plan{i}.docx", range(2)) for i in range(4))),
             {f"d{i}": 0.1 * (i + 1) for i in range(4)})
    acme = (ACME, _result(*(_file("b2", f"x{i}", f"Acme/review{i}.docx", range(2)) for i in range(4))),
            {f"x{i}": 0.15 + 0.1 * i for i in range(4)})
    return [drive, acme]


def test_the_merge_keeps_k_chunks_by_default_and_twice_as_many_wide():
    narrow = _merge(_two_drives())
    wide = _merge(_two_drives(), caps=ExcerptCaps.WIDE)
    assert sum(len(f.chunks) for f in narrow.files) == EXCERPTS_K
    assert sum(len(f.chunks) for f in wide.files) == 16   # every chunk of both parts: under the wide k
    assert narrow.more is True and wide.more is False


def test_more_comes_from_a_part_when_the_merge_dropped_nothing():
    drive = (DRIVE, _result(_file("a1", "d1", "Drive/plan.docx", (0,)), more=True), {"d1": 0.2})
    acme = (ACME, _result(_file("b2", "x1", "Acme/review.docx", (0,))), {"x1": 0.1})
    assert _merge([drive, acme]).more is True
    assert _merge([(DRIVE, replace(drive[1], more=False), drive[2]), acme]).more is False


def test_an_unreadable_parts_more_does_not_count():
    drive = (DRIVE, _result(_file("a1", "d1", "Drive/plan.docx", (0,))), {"d1": 0.2})
    off = (ACME, ExcerptResult(status=STATUS_DISABLED, request=REQUEST, more=True), {})
    merged = _merge([drive, off])
    assert merged.more is False and merged.not_searched_drives == ("Acme",)


# -- search: a tier showed fewer than it matched --------------------------------------------------------------------

def test_a_tier_with_members_past_its_shown_count_is_more():
    r = example()
    about = r.tiered.block(MatchTier.ABOUT)
    rest = (GroupMember("Insurance", "pdf", "2026-01"),)
    tiered = replace(r.tiered, blocks=tuple(replace(b, rest=rest) if b is about else b for b in r.tiered.blocks))
    text, more = rendered_results(replace(r, tiered=tiered))
    assert more is True and text == render_results(replace(r, tiered=tiered))
    assert rendered_results(replace(r, tiered=replace(tiered, blocks=tuple(
        replace(b, rest=()) for b in tiered.blocks))))[1] is False


def test_related_members_alone_are_not_more():
    r = example()
    blocks = tuple(replace(b, rest=(GroupMember("Travel", "pdf", "2026-05"),)) if b.tier is MatchTier.RELATED
                   else replace(b, rest=()) for b in r.tiered.blocks)
    assert tiered_text(replace(r, tiered=replace(r.tiered, blocks=blocks)))[1] is False


def _big():
    r = example()
    big = "x" * 690
    about = tuple(card(f"A{i}", f"Docs/a{i}.pdf", big, MatchTier.ABOUT, sources=('name "rome"',), passage="p" * 300)
                  for i in range(ABOUT_SHOWN))
    labelled = tuple(card(f"L{i}", f"Docs/l{i}.pdf", big, MatchTier.LABELLED, passage="q" * 900) for i in range(8))
    tiered = replace(r.tiered, blocks=(TierBlock(MatchTier.ABOUT, ABOUT_SHOWN), TierBlock(MatchTier.LABELLED, 8)))
    return replace(r, cards=about + labelled, tiered=tiered)


def test_cards_the_character_cut_moved_are_more_and_the_wide_cap_keeps_them():
    r = _big()
    narrow, narrow_more = rendered_results(r)
    wide, wide_more = rendered_results(r, caps=SearchCaps.WIDE)
    assert "Shortened to fit" in narrow and narrow_more is True
    assert len(narrow) <= RESULT_CHARS_MAX < len(wide) <= SearchCaps.WIDE.result_chars
    assert "Shortened to fit" not in wide and wide_more is False
    assert render_tiered(r) == narrow and render_tiered(r, chars_max=SearchCaps.WIDE.result_chars) == wide


def test_a_result_without_tiers_is_never_more():
    from src.domain.drive_search import DriveSearchResult
    assert rendered_results(DriveSearchResult(status=STATUS_DISABLED))[1] is False


# -- the arguments ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["yes", "YES", " Yes ", "true", "True", "1", True])
def test_wide_forms(value):
    assert parse_wide(value) is True


@pytest.mark.parametrize("value", ["", None, "no", "false", "0", "2", "wide", "yes please", False])
def test_anything_else_is_the_default_never_an_error(value):
    assert parse_wide(value) is False


@pytest.mark.parametrize("value, number", [("", 1), (None, 1), ("1", 1), ("2", 2), (" 3 ", 3), (4, 4)])
def test_a_batch_is_a_positive_whole_number_empty_the_first(value, number):
    assert parse_batch(value) == number


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5", "2nd", 0, -2, True])
def test_a_batch_that_is_not_a_positive_integer_is_a_request_error_with_its_correction(value):
    with pytest.raises(ValueError) as caught:
        parse_batch(value)
    assert str(caught.value) == BAD_BATCH.format(value=str(value).strip())
