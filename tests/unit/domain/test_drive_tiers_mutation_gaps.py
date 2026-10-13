"""Boundaries of the Search v2 tiers that mutation testing showed unpinned (mutation_sv2.md, drive_tiers): the
concept weight, the absent/common/low-weight allowances, cover ties, the subset-search cap, the best match, the
evidence spans and the robust z (USER_DRIVE_INDEXING_RFC §25.5.3, §25.5.4)."""
import math

import pytest

from src.domain.drive_enrichment import HintItem
from src.domain.drive_index_params import COMMON_SHARE, MEANING_POOL, MIN_SPREAD
from src.domain.drive_keys import family_keys, query_concepts, word_form_keys, word_keys
from src.domain.drive_match import Concept, MatchTier, SourceClass
from src.domain.drive_tiers import (
    _SUBSET_SEARCH_MAX, WHERE_CHUNK, WHERE_HINT, WHERE_RECORD, Allowance, Cover, FileKeys, allowances, chunk_cover,
    classify, concept_weight, cover_spans, record_cover, source_scores,
)


def concept(word: str) -> Concept:
    (c,) = query_concepts(word).concepts
    return c


# -- weight and allowances -------------------------------------------------------------------------------------------

def test_the_concept_weight():
    assert concept_weight(1, 10) == pytest.approx(math.log(11))
    assert concept_weight(5, 0) == pytest.approx(math.log(1.2))     # an empty drive counts as one carded file
    assert concept_weight(0, 10) == math.inf


def test_a_word_missing_from_the_frequencies_is_absent():
    cs = [concept("rome"), concept("tickets")]
    assert allowances(cs, {"rome": 2}, 100).absent == ("tickets",)


def test_common_starts_strictly_above_the_share():
    cs = [concept("photo"), concept("rome")]
    at = int(COMMON_SHARE * 10)
    assert allowances(cs, {"photo": at, "rome": 1}, 10).common == frozenset()
    assert allowances(cs, {"photo": at + 1, "rome": 1}, 10).common == frozenset({"photo"})


def test_no_low_weight_word_when_every_word_is_absent():
    cs = [concept("alpha"), concept("bravo"), concept("charlie")]
    assert allowances(cs, {}, 100).low_weight is None


def test_the_low_weight_word_is_the_most_frequent():
    cs = [concept("alpha"), concept("bravo"), concept("charlie")]
    assert allowances(cs, {"alpha": 5, "bravo": 9, "charlie": 1}, 100).low_weight == "bravo"
    assert allowances(cs[:2], {"alpha": 5, "bravo": 9}, 100).low_weight is None   # two words: nothing missable


# -- covers ----------------------------------------------------------------------------------------------------------

def test_a_tie_between_about_and_name_keeps_the_about_key():
    c = concept("rome")
    file = FileKeys("x", about_keys=frozenset({"e:rome"}), name_keys=frozenset({"w:rome"}))
    cover = record_cover(file, c)
    assert cover.where == WHERE_RECORD and cover.source_class is SourceClass.VERIFIED_RECORD


def test_a_chunks_own_words_beat_its_hint_keys():
    c = concept("rome")
    cid = ("x", 1, 0)
    file = FileKeys("x", chunk_keys={cid: frozenset(word_keys("in Rome"))}, hint_keys={cid: frozenset({"w:rome"})})
    cover = chunk_cover(file, c, cid)
    assert cover.where == WHERE_CHUNK and cover.source_class is SourceClass.PASSAGE_WORDS


def test_a_hint_beats_a_skeleton_in_the_same_chunk():
    c = concept("рим")
    cid = ("x", 1, 0)
    skeleton = next(k for k in c.word_keys if k.startswith(("x:", "xl:")))
    file = FileKeys("x", chunk_keys={cid: frozenset({skeleton})}, hint_keys={cid: frozenset({"w:рим"})})
    assert chunk_cover(file, c, cid).where == WHERE_HINT


def test_evidence_spans_need_a_stored_span_and_the_keyword():
    cover = Cover("rome", SourceClass.TRANSLATION, WHERE_HINT, tuple(word_form_keys("Rome")))
    items = [HintItem("Rome", "keyword", "en Roma", 0, 7), HintItem("Rome", "keyword", "x", -1, -1),
             HintItem("Paris", "keyword", "a Paris", 10, 17)]
    assert cover_spans(cover, "en Roma y a Paris", items) == ((0, 7),)


def test_a_tie_between_record_and_chunk_needs_no_chunk():
    """A concept the record covers as well as the chunk does (both translations) is not put in the chunk: the match
    keeps record-only covers and no passage."""
    c = concept("рим")
    cid = ("x", 1, 0)
    ex = next(k for k in c.about_keys if k.startswith("ex"))
    x = next(k for k in c.word_keys if k.startswith("x"))
    file = FileKeys("x", about_keys=frozenset({ex}), chunk_keys={cid: frozenset({x})})
    match = classify(file, [c], allowances([c], {c.word: 1}, 10), {}, {})
    assert match is not None and match.chunk is None and match.tier is MatchTier.TRANSLATED


def test_a_file_may_miss_the_low_weight_word():
    cs = [concept("alpha"), concept("bravo"), concept("charlie")]
    file = FileKeys("x", about_keys=frozenset({"e:alpha", "e:charli"}))
    allowance = allowances(cs, {"alpha": 1, "bravo": 9, "charlie": 1}, 100)
    match = classify(file, cs, allowance, {}, {})
    assert match is not None and match.missed == (("bravo", "low_weight"),)


@pytest.mark.parametrize("allowance,concepts", [(Allowance(absent=("x",)), [concept("rome")]), (Allowance(), [])])
def test_no_match_with_an_absent_word_or_without_words(allowance, concepts):
    assert classify(FileKeys("x", about_keys=frozenset({"e:rome"})), concepts, allowance, {}, {}) is None


def test_the_best_chunk_wins_not_the_last():
    c = concept("rome")
    good, worse = ("x", 1, 0), ("x", 1, 1)
    skeleton = next(k for k in concept("рим").word_keys if k.startswith("x"))
    c2 = Concept("rome", c.about_keys, c.word_keys + (skeleton,), c.hint_keys, False)
    file = FileKeys("x", chunk_keys={good: frozenset(word_keys("Rome")), worse: frozenset({skeleton})})
    match = classify(file, [c2], allowances([c2], {"rome": 1}, 10), {}, {})
    assert match.tier is MatchTier.MENTIONED and match.chunk == good


# -- the subset search cap -------------------------------------------------------------------------------------------

_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliett", "kilo",
          "lima"]


def _spread_file(n: int):
    """n words, each a label word on the record (automatic) and in one chunk (passage words): all n but the last
    sit together, the last one far away, so "every word in the chunk" fails proximity and "none" is record-only."""
    words = _WORDS[:n]
    text = " ".join(words[:-1]) + " " + "filler " * 120 + words[-1]
    cid = ("x", 1, 0)
    about = frozenset(k for w in words for k in family_keys("lw:", [w]))
    file = FileKeys("x", about_keys=about, chunk_keys={cid: frozenset(word_keys(text))})
    cs = [concept(w) for w in words]
    return file, cs, {cid: text}


def test_the_subset_search_cap_is_ten_optional_words():
    assert _SUBSET_SEARCH_MAX == 10


def test_up_to_the_cap_the_best_subset_of_words_is_put_in_the_chunk():
    file, cs, texts = _spread_file(10)
    match = classify(file, cs, allowances(cs, {c.word: 1 for c in cs}, 100), texts, {})
    assert match.chunk is not None
    assert sum(1 for cv in match.covers if cv.where == WHERE_CHUNK) == 9


def test_past_the_cap_only_all_or_none_is_tried():
    file, cs, texts = _spread_file(11)
    match = classify(file, cs, allowances(cs, {c.word: 1 for c in cs}, 100), texts, {})
    assert match.chunk is None and match.tier is MatchTier.LABELLED


# -- the robust z ---------------------------------------------------------------------------------------------------

def test_the_robust_z_uses_the_normal_consistent_mad():
    sims = {f"f{i:02d}": i / 100 for i in range(13)}     # 0.00 .. 0.12: median 0.06, MAD 0.03
    z = source_scores(sims)
    assert z["f12"] == pytest.approx((0.12 - 0.06) / (1.4826 * 0.03))
    assert z["f06"] == pytest.approx(0.0)


def test_a_flat_pool_uses_the_minimum_spread():
    z = source_scores({f"f{i:02d}": 0.5 for i in range(12)} | {"top": 0.5 + MIN_SPREAD})
    assert z["top"] == pytest.approx(1.0)


def test_the_pool_keeps_the_best_files_ties_by_item_id():
    sims = {f"f{i:03d}": 0.5 for i in reversed(range(MEANING_POOL + 1))}   # inserted last id first
    z = source_scores(sims)
    assert len(z) == MEANING_POOL and f"f{MEANING_POOL:03d}" not in z and "f000" in z
