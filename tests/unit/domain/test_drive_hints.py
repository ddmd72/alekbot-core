"""drive_hints (USER_DRIVE_INDEXING_RFC §25.4.2, §25.4.7): grounding of passage hints against their chunk — the evidence
quote verbatim (after NFC, casefold, whitespace collapse) with its span, numbers among the chunk's number terms,
plain mentions add no hint keys, a far set is dropped whole — and the reuse matching of an edited file's chunks.
Synthetic texts; the keyword keyer is injected (P1's `word_form_keys` by signature)."""
import unicodedata

import pytest

from src.domain import drive_hints as dh
from src.domain.drive_enrichment import UNGROUNDED, HintItem
from src.domain.drive_index_params import (
    HINT_KEYS_PER_CHUNK, HINT_KEYWORDS_MAX, HINT_QUESTIONS_MAX, PASSAGE_MIN_SIMILARITY,
)

CHUNK = ("Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo de 2026.\n\n"
         "Importe   total: 1.250,00 €. Localizador ABX-7781.")


def kw(text, evidence):
    return HintItem(text=text, type="keyword", evidence=evidence)


def q(text, evidence):
    return HintItem(text=text, type="question", evidence=evidence)


def keys(text):
    """A stand-in keyer: one `w:` key per lower-cased token."""
    return tuple(f"w:{t}" for t in text.casefold().replace(",", " ").split())


class TestEvidence:
    def test_a_verbatim_quote_is_grounded_with_its_span(self):
        g = dh.ground(CHUNK, [kw("Rome", "reserva en Roma, 3 noches")])
        item = g.items[0]
        assert CHUNK[item.evidence_start:item.evidence_end] == "reserva en Roma, 3 noches"

    def test_case_and_whitespace_are_collapsed_and_the_span_covers_the_original(self):
        g = dh.ground(CHUNK, [kw("total", "IMPORTE TOTAL: 1.250,00 €")])
        item = g.items[0]
        assert CHUNK[item.evidence_start:item.evidence_end] == "Importe   total: 1.250,00 €"

    def test_nfc_equivalent_forms_match(self):
        chunk = unicodedata.normalize("NFD", "Café da Praça, Lisboa")
        g = dh.ground(chunk, [kw("Lisbon", unicodedata.normalize("NFC", "Café da Praça"))])
        assert len(g.items) == 1
        assert unicodedata.normalize("NFC", chunk[g.items[0].evidence_start:g.items[0].evidence_end]) == \
            "Café da Praça"

    def test_surrounding_quotes_and_ellipses_are_not_part_of_the_quote(self):
        g = dh.ground(CHUNK, [kw("Rome", '"…reserva en Roma…"')])
        assert len(g.items) == 1 and CHUNK[g.items[0].evidence_start:g.items[0].evidence_end] == "reserva en Roma"

    @pytest.mark.parametrize("evidence", ["reserva en Oporto", "", "   ", "reserva  en  Roma, 4 noches"])
    def test_a_quote_not_in_the_chunk_drops_the_item(self, evidence):
        g = dh.ground(CHUNK, [kw("Rome", evidence)])
        assert g.items == () and g.drops.evidence == 1

    def test_the_model_reply_starts_ungrounded(self):
        assert (kw("x", "y").evidence_start, kw("x", "y").evidence_end) == (UNGROUNDED, UNGROUNDED)


class TestNumbers:
    def test_a_number_in_the_chunk_passes(self):
        g = dh.ground(CHUNK, [kw("1250 EUR booking", "Importe total: 1.250,00 €"),
                              q("When is the stay, 13 May 2026?", "del 10 al 13 de mayo de 2026")])
        assert len(g.items) == 2 and g.drops.numbers == 0

    @pytest.mark.parametrize("text", ["total 1350 EUR", "booking ABX-7782", "stay on 2026-05-11"])
    def test_a_number_not_in_the_chunk_drops_the_item(self, text):
        g = dh.ground(CHUNK, [kw(text, "Importe total: 1.250,00 €")])
        assert g.items == () and g.drops.numbers == 1

    def test_a_date_in_another_language_is_the_same_term(self):
        g = dh.ground(CHUNK, [q("Что было 13 мая 2026?", "del 10 al 13 de mayo de 2026")])
        assert len(g.items) == 1


class TestHintKeys:
    def test_hint_keys_are_the_keywords_forms_not_in_the_chunk(self):
        g = dh.ground(CHUNK, [kw("Rome Roma", "reserva en Roma"), q("Where is the hotel?", "reserva en Roma")],
                      chunk_word_keys=("w:roma", "w:reserva"), keyword_keys=keys)
        assert g.hint_keys == ("w:rome",)

    def test_a_plain_mention_adds_no_keys_and_is_counted(self):
        g = dh.ground(CHUNK, [kw("Roma", "reserva en Roma")], chunk_word_keys=("w:roma",), keyword_keys=keys)
        assert g.hint_keys == () and g.drops.plain == 1 and len(g.items) == 1

    def test_questions_give_no_keys(self):
        g = dh.ground(CHUNK, [q("Hotel in Rome?", "reserva en Roma")], chunk_word_keys=(), keyword_keys=keys)
        assert g.hint_keys == ()

    def test_hint_keys_are_capped_earliest_first(self):
        items = [kw(" ".join(f"k{chr(97 + i)}x{chr(97 + j)}" for j in range(10)), "reserva en Roma") for i in range(10)]
        g = dh.ground(CHUNK, items, chunk_word_keys=(), keyword_keys=keys)
        assert len(g.hint_keys) == HINT_KEYS_PER_CHUNK and g.hint_keys[0] == "w:kaxa"

    def test_without_a_keyer_no_keys(self):
        assert dh.ground(CHUNK, [kw("Rome", "reserva en Roma")]).hint_keys == ()


class TestShape:
    def test_caps_and_duplicates(self):
        items = ([kw(f"word{chr(97 + i)}", "reserva en Roma") for i in range(20)] + [kw("worda", "en Roma")]
                 + [q(f"question {chr(97 + i)}?", "reserva en Roma") for i in range(5)])
        g = dh.ground(CHUNK, items)
        assert sum(i.type == "keyword" for i in g.items) == HINT_KEYWORDS_MAX
        assert sum(i.type == "question" for i in g.items) == HINT_QUESTIONS_MAX

    def test_an_unknown_type_or_empty_text_is_dropped(self):
        g = dh.ground(CHUNK, [HintItem("x", "summary", "reserva en Roma"), kw("  ", "reserva en Roma")])
        assert g.items == () and g.drops.invalid == 2


class TestSimilarity:
    def test_a_far_set_is_dropped_whole(self):
        g = dh.ground(CHUNK, [kw("Rome", "reserva en Roma")], chunk_word_keys=(), keyword_keys=keys)
        far = dh.drop_if_far(g, PASSAGE_MIN_SIMILARITY - 0.01)
        assert far.items == () and far.hint_keys == () and far.drops.far == 1

    def test_a_near_set_stays(self):
        g = dh.ground(CHUNK, [kw("Rome", "reserva en Roma")])
        assert dh.drop_if_far(g, PASSAGE_MIN_SIMILARITY) is g


class TestReuse:
    def test_the_same_text_hash(self):
        old = [dh.old_chunk(0, "other text"), dh.old_chunk(1, CHUNK)]
        assert dh.reuse_source(CHUNK, old) == 1

    def test_most_paragraphs_from_one_old_chunk(self):
        paras = [f"Paragraph number {i} about the trip." for i in range(10)]
        old = [dh.old_chunk(3, "\n\n".join(paras[:9] + ["An old ending."]))]
        new = "\n\n".join(paras[:8] + ["A new line.", paras[8]])
        assert dh.reuse_source(new, old) == 3       # 9 of 10 paragraphs (90 %) came from chunk 3

    def test_under_the_share_nothing_is_reused(self):
        paras = [f"Paragraph number {i}." for i in range(10)]
        old = [dh.old_chunk(0, "\n\n".join(paras[:5])), dh.old_chunk(1, "\n\n".join(paras[5:]))]
        assert dh.reuse_source("\n\n".join(paras), old) is None

    def test_whitespace_does_not_change_a_paragraph(self):
        assert dh.paragraph_hashes("A  b\n c\n\n\nD") == dh.paragraph_hashes("A b c\n\nD")

    def test_reused_items_are_grounded_again_with_new_spans(self):
        old_items = dh.ground(CHUNK, [kw("Rome", "reserva en Roma")]).items
        moved = "Nota previa.\n\n" + CHUNK
        again = dh.ground(moved, old_items)
        assert moved[again.items[0].evidence_start:again.items[0].evidence_end] == "reserva en Roma"
        assert again.items[0].evidence_start != old_items[0].evidence_start
