"""drive_tiers — Search v2's pure tier decision (USER_DRIVE_INDEXING_RFC §25.5.3, §25.5.4).

One test per combination of source classes (§25.5.4: "One test per combination of classes is required"), strict AND
with its allowances (absent, low weight, common), the minimum window, and the meaning score with abstention and the
long-document correction. Keys come from the real keyer (`drive_keys`), so index/query parity is exercised too."""
import math

import pytest

from src.domain.drive_enrichment import HintItem
from src.domain.drive_keys import name_keys_v2, query_concepts, word_form_keys, word_keys
from src.domain.drive_match import MatchTier, SourceClass
from src.domain.drive_index_params import (
    COMMON_SHARE, MEANING_POOL, MEANING_POOL_MIN, PROXIMITY_CHARS, Z_RELATED,
)
from src.domain.drive_tiers import (
    Allowance, FileKeys, allowances, chunks_needing_text, classify, concept_weight, long_doc_correction,
    meaning_scores, min_window, related_floor, size_band, source_scores,
)

C1 = ("f", 1, 0)
C2 = ("f", 1, 1)


def concepts(query):
    return query_concepts(query).concepts


def keys(*items):
    return frozenset(items)


def file(about=(), name=(), chunks=None, hints=None):
    return FileKeys(item_id="f", about_keys=frozenset(about), name_keys=frozenset(name),
                    chunk_keys={k: frozenset(v) for k, v in (chunks or {}).items()},
                    hint_keys={k: frozenset(v) for k, v in (hints or {}).items()})


NONE = Allowance()


def run(f, query, allowance=NONE, texts=None, items=None):
    return classify(f, concepts(query), allowance, texts or {}, items or {})


# -- one test per combination of source classes ---------------------------------------------------------------------

class TestSourceClassCombinations:
    def test_verified_record_only_is_about(self):
        m = run(file(about=["e:коїмбра", "e:коїмбр"]), "про Коїмбру")
        assert m.tier is MatchTier.ABOUT and m.covers[0].source_class is SourceClass.VERIFIED_RECORD

    def test_name_alone_is_about(self):
        m = run(file(name=name_keys_v2("Receipt 2023.pdf")), "receipts 2023")
        assert m.tier is MatchTier.ABOUT and {c.where for c in m.covers} == {"name"}

    def test_passage_words_only_is_mentioned(self):
        text = "Ми гуляли в Коїмбрі весь день."
        m = run(file(chunks={C1: word_keys(text)}), "про Коїмбру", texts={C1: text})
        assert m.tier is MatchTier.MENTIONED and m.chunk == C1

    def test_verified_plus_passage_is_mentioned(self):
        text = "We bought the tickets at the station."
        m = run(file(about=["e:rome"], chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text})
        assert m.tier is MatchTier.MENTIONED
        assert {(c.word, c.where) for c in m.covers} == {("rome", "record"), ("tickets", "chunk")}

    def test_skeleton_in_a_chunk_is_translation(self):
        text = "Notes from the Coimbra trip."
        m = run(file(chunks={C1: word_keys(text)}), "про Коїмбру", texts={C1: text})
        assert m.tier is MatchTier.TRANSLATED and m.covers[0].source_class is SourceClass.TRANSLATION

    def test_hint_keyword_is_translation(self):
        text = "Confirmamos su reserva en Roma, 3 noches."
        m = run(file(hints={C1: word_form_keys("Rome")}), "rome", texts={C1: text})
        assert m.tier is MatchTier.TRANSLATED and m.covers[0].where == "hint"

    def test_entity_skeleton_in_the_record_is_translation(self):
        m = run(file(about=["ex:koimbr"]), "про Коїмбру")
        assert m.tier is MatchTier.TRANSLATED and m.covers[0].where == "record"

    def test_name_skeleton_is_translation(self):
        m = run(file(name=["x:koimbr", "w:coimbra"]), "про Коїмбру")
        assert m.tier is MatchTier.TRANSLATED and m.covers[0].where == "name"

    def test_automatic_record_only_is_labelled(self):
        m = run(file(about=["lw:receipt", "lw:receipts"]), "all my receipts")
        assert m.tier is MatchTier.LABELLED and m.covers[0].source_class is SourceClass.AUTOMATIC_RECORD

    def test_verified_plus_automatic_is_labelled(self):
        m = run(file(about=["e:rome", "lw:ticket"]), "rome tickets")
        assert m.tier is MatchTier.LABELLED

    def test_passage_plus_automatic_is_labelled(self):
        text = "Packing list for Rome."
        m = run(file(about=["lw:ticket"], chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text})
        assert m.tier is MatchTier.LABELLED

    def test_translation_plus_automatic_is_labelled(self):
        m = run(file(about=["ex:koimbr", "tw:trip"]), "Коїмбра trip")
        assert m.tier is MatchTier.LABELLED

    def test_verified_plus_translation_is_translated(self):
        m = run(file(about=["e:trip", "ex:koimbr"]), "Коїмбра trip")
        assert m.tier is MatchTier.TRANSLATED

    def test_passage_plus_hint_in_the_same_chunk_is_translated(self):
        text = "Reserva en Roma. Recoger los billetes el martes."
        f = file(chunks={C1: word_keys(text)}, hints={C1: word_form_keys("tickets")})
        items = {C1: (HintItem("tickets", "keyword", "Recoger los billetes", 17, 37),)}
        m = run(f, "roma tickets", texts={C1: text}, items=items)
        assert m.tier is MatchTier.TRANSLATED and m.chunk == C1

    def test_the_best_class_wins_per_concept(self):
        m = run(file(about=["e:rome", "lw:rome"]), "rome")
        assert m.tier is MatchTier.ABOUT

    def test_record_keys_of_other_families_never_cover(self):
        assert run(file(about=["l:type:receipt"]), "receipt") is None


# -- strict AND and the allowances ----------------------------------------------------------------------------------

class TestStrictAnd:
    def test_an_unmatched_concept_blocks(self):
        assert run(file(about=["e:rome"]), "rome tickets") is None

    def test_absent_concept_blocks_even_when_it_would_be_low_weight(self):
        cs = concepts("rome trip tickets")
        allowance = allowances(cs, {"rome": 3, "trip": 0, "tickets": 40}, carded=100)
        assert allowance.absent == ("trip",)
        assert classify(file(about=["e:rome", "lw:ticket"]), cs, allowance, {}, {}) is None

    def test_low_weight_word_may_be_missed_with_three_concepts(self):
        cs = concepts("rome colosseum tickets")
        allowance = allowances(cs, {"rome": 5, "colosseum": 2, "tickets": 20}, carded=1000)
        assert allowance.low_weight == "tickets"
        m = classify(file(about=["e:rome", "e:colosseum"]), cs, allowance, {}, {})
        assert m.tier is MatchTier.ABOUT and m.missed == (("tickets", "low_weight"),)

    def test_no_low_weight_allowance_with_two_concepts(self):
        cs = concepts("rome tickets")
        assert allowances(cs, {"rome": 5, "tickets": 20}, carded=1000).low_weight is None

    def test_only_one_word_may_be_missed_by_weight(self):
        cs = concepts("rome colosseum tickets")
        allowance = allowances(cs, {"rome": 5, "colosseum": 2, "tickets": 20}, carded=1000)
        assert classify(file(about=["e:colosseum"]), cs, allowance, {}, {}) is None

    def test_common_word_may_be_missed_with_any_number_of_concepts(self):
        cs = concepts("rome photo")
        carded = 100
        allowance = allowances(cs, {"rome": 3, "photo": int(carded * COMMON_SHARE) + 1}, carded=carded)
        assert allowance.common == frozenset({"photo"})
        m = classify(file(about=["e:rome"]), cs, allowance, {}, {})
        assert m.tier is MatchTier.ABOUT and m.missed == (("photo", "common"),)

    def test_a_lone_common_word_is_still_required(self):
        cs = concepts("photo")
        allowance = allowances(cs, {"photo": 90}, carded=100)
        assert allowance.common == frozenset()

    def test_covering_beats_missing(self):
        """A file matching every word, even automatically, is not shown as missing one (fewest misses first)."""
        cs = concepts("rome photo")
        allowance = allowances(cs, {"rome": 3, "photo": 80}, carded=100)
        m = classify(file(about=["e:rome", "pw:photo"]), cs, allowance, {}, {})
        assert m.tier is MatchTier.LABELLED and m.missed == ()

    def test_weight_is_log_of_inverse_share(self):
        assert concept_weight(10, 100) == pytest.approx(math.log(1 + 100 / 10))


# -- proximity (minimum window) ---------------------------------------------------------------------------------------

class TestProximity:
    def test_two_words_close_together_are_mentioned(self):
        text = "pick up the tickets for the Vatican museums in Rome on Tuesday"
        m = run(file(chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text})
        assert m.tier is MatchTier.MENTIONED

    def test_two_words_far_apart_do_not_count(self):
        text = "tickets " + "x" * (PROXIMITY_CHARS + 50) + " Rome"
        assert run(file(chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text}) is None

    def test_far_apart_falls_back_to_the_record_class(self):
        text = "tickets " + "x" * (PROXIMITY_CHARS + 50) + " Rome"
        m = run(file(about=["lw:ticket"], chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text})
        assert m.tier is MatchTier.LABELLED and {c.where for c in m.covers} == {"record", "chunk"}

    def test_another_chunk_can_hold_them_together(self):
        far = "tickets " + "x" * (PROXIMITY_CHARS + 50) + " Rome"
        near = "Rome tickets"
        f = file(chunks={C1: word_keys(far), C2: word_keys(near)})
        m = run(f, "rome tickets", texts={C1: far, C2: near})
        assert m.tier is MatchTier.MENTIONED and m.chunk == C2

    def test_record_covered_concepts_are_exempt(self):
        text = "tickets " + "x" * (PROXIMITY_CHARS + 50)
        m = run(file(about=["e:rome"], chunks={C1: word_keys(text)}), "rome tickets", texts={C1: text})
        assert m.tier is MatchTier.MENTIONED

    def test_a_hint_concept_uses_its_evidence_span(self):
        text = "Recoger los billetes. " + "x" * (PROXIMITY_CHARS + 50) + " Roma"
        f = file(chunks={C1: word_keys(text)}, hints={C1: word_form_keys("tickets")})
        items = {C1: (HintItem("tickets", "keyword", "Recoger los billetes", 0, 20),)}
        assert run(f, "roma tickets", texts={C1: text}, items=items) is None

    def test_a_chunk_without_text_fails_proximity(self):
        text = "Rome tickets"
        assert run(file(chunks={C1: word_keys(text)}), "rome tickets", texts={}) is None

    def test_chunks_needing_text_are_those_with_two_chunk_concepts(self):
        f = file(about=["e:rome"], chunks={C1: word_keys("Rome tickets"), C2: word_keys("tickets only")})
        assert chunks_needing_text(f, concepts("rome tickets")) == ()
        f = file(chunks={C1: word_keys("Rome tickets"), C2: word_keys("tickets only")})
        assert chunks_needing_text(f, concepts("rome tickets")) == (C1,)

    def test_min_window(self):
        assert min_window([[(0, 4)], [(10, 14)]]) == 14
        assert min_window([[(0, 4), (100, 104)], [(90, 95)]]) == 14
        assert min_window([[(0, 4)], []]) is None


# -- meaning (§25.5.3) ----------------------------------------------------------------------------------------------

class TestMeaning:
    def _pool(self, n, top=None):
        sims = {f"f{i:03d}": 0.30 + 0.001 * i for i in range(n)}
        if top is not None:
            sims["top"] = top
        return sims

    def test_a_source_with_too_few_files_abstains(self):
        assert source_scores(self._pool(MEANING_POOL_MIN - 1)) is None
        assert source_scores(self._pool(MEANING_POOL_MIN)) is not None

    def test_an_outlier_scores_high(self):
        z = source_scores(self._pool(30, top=0.9))
        assert z["top"] > Z_RELATED and z["f000"] < 0

    def test_the_pool_is_the_best_meaning_pool_files(self):
        z = source_scores(self._pool(MEANING_POOL + 20))
        assert len(z) == MEANING_POOL and "f000" not in z

    def test_long_document_correction(self):
        assert long_doc_correction(4) == 0 and long_doc_correction(1) == 0
        assert long_doc_correction(100) == pytest.approx(math.sqrt(2 * math.log(100)) - math.sqrt(2 * math.log(4)))

    def test_meaning_is_the_max_over_sources_with_the_correction_on_chunks_and_hints(self):
        card = {"a": 1.0}
        chunk = {"a": 3.0, "b": 4.0}
        out = meaning_scores(card, chunk, {}, chunk_counts={"b": 100}, no_chunk_score=frozenset())
        assert out["a"] == 3.0 and out["b"] == pytest.approx(4.0 - long_doc_correction(100))

    def test_no_chunk_score_for_excluded_files(self):
        out = meaning_scores({"a": 1.0}, {"a": 5.0}, {"a": 5.0}, chunk_counts={}, no_chunk_score=frozenset({"a"}))
        assert out["a"] == 1.0

    def test_bands_and_floor(self):
        assert [size_band(n) for n in (0, 199, 200, 1999, 2000, 9999, 10000)] == [0, 0, 1, 1, 2, 2, 3]
        assert related_floor(size_band(50)) == Z_RELATED
