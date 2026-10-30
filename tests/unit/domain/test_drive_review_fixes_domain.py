"""Search v2 P3 review fixes in the pure domain (USER_DRIVE_INDEXING_RFC §25.3, §25.4.1, §25.4.2):
- M3: grounding rule 1 needs a real quote — at least 3 words or 12 characters, at most HINT_EVIDENCE_WORDS_MAX words,
  matched on word boundaries;
- m5: an id written in another script passes rule 2 when its digit runs are the chunk's;
- m10: a keyword or question longer than its cap is dropped;
- m4: only short notes get a template; a short receipt or ticket is profiled by the model;
- m7: verify caps life domains at three and keeps a key value's maximal form, never a fragment."""
import pytest

from src.domain import drive_hints as dh
from src.domain import drive_profile as dp
from src.domain.drive_enrichment import DriveProfile, HintItem, KeyValue
from src.domain.drive_index import DriveChunk, DriveIndexRecord

CHUNK = "Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo de 2026. Modelo F-150 en garaje."


def kw(text, evidence):
    return HintItem(text=text, type="keyword", evidence=evidence)


class TestEvidenceIsAQuote:
    def test_a_single_letter_does_not_ground_passport_renewal(self):
        """The reviewer's case: evidence "a" sat inside "Confirmamos" and kept a hallucinated keyword."""
        g = dh.ground(CHUNK, [kw("passport renewal", "a")])
        assert g.items == () and g.drops.evidence == 1

    @pytest.mark.parametrize("evidence", ["en Roma", "Roma", "3 noches"])
    def test_under_three_words_and_twelve_characters_is_not_a_quote(self, evidence):
        assert dh.ground(CHUNK, [kw("Rome", evidence)]).items == ()

    @pytest.mark.parametrize("evidence", ["reserva en Roma", "Confirmamos su"])
    def test_three_words_or_twelve_characters_is_a_quote(self, evidence):
        assert len(dh.ground(CHUNK, [kw("booking", evidence)]).items) == 1

    def test_a_match_inside_a_word_is_not_a_quote(self):
        assert dh.ground(CHUNK, [kw("firm", "firmamos su reserva")]).items == ()

    def test_a_later_whole_word_occurrence_still_grounds(self):
        chunk = "reservation desk: la reserva en Roma"
        g = dh.ground(chunk, [kw("Rome", "reserva en Roma")])
        assert chunk[g.items[0].evidence_start:g.items[0].evidence_end] == "reserva en Roma"

    def test_more_than_twelve_words_is_not_a_quote(self):
        long = "Confirmamos su reserva en Roma, 3 noches, del 10 al 13 de mayo de 2026."
        g = dh.ground(CHUNK, [kw("Rome", long)])
        assert g.items == () and g.drops.evidence == 1


class TestIdsInAnotherScript:
    def test_a_cyrillic_id_meets_its_latin_spelling_by_its_digits(self):
        assert len(dh.ground(CHUNK, [kw("Ф-150", "Modelo F-150 en garaje")]).items) == 1

    def test_digits_not_in_the_chunk_still_drop(self):
        g = dh.ground(CHUNK, [kw("Ф-151", "Modelo F-150 en garaje")])
        assert g.items == () and g.drops.numbers == 1


class TestTextCaps:
    def test_a_sentence_as_a_keyword_is_dropped(self):
        g = dh.ground(CHUNK, [kw("word " * 20, "reserva en Roma")])
        assert g.items == () and g.drops.invalid == 1

    def test_an_overlong_question_is_dropped(self):
        g = dh.ground(CHUNK, [HintItem("Why " * 60 + "?", "question", "reserva en Roma")])
        assert g.items == () and g.drops.invalid == 1


def _chunks(text):
    return [DriveChunk("u", "i", 1, 0, text, 0, len(text), "pdf")]


class TestShortFiles:
    @pytest.mark.parametrize("kind", ["pdf", "docx", "eml"])
    def test_a_short_document_is_profiled_by_the_model(self, kind):
        assert dp.policy_of(kind, text_chars=120) == dp.PROFILE_POLICY[kind]
        rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind=kind)
        text = "Ticket 4411. Bus Springfield-Lisbon. 12/05/2026 08:30. Seat 14."
        assert dp.profile_input(rec, _chunks(text), kind).head == text

    def test_a_short_scan_is_profiled_by_the_model(self):
        assert dp.policy_of("image", text_chars=80, triaged_text=True) == dp.PROFILE_POLICY["pdf"]

    @pytest.mark.parametrize("kind", ["markdown", "text"])
    def test_only_a_short_note_gets_the_template(self, kind):
        assert dp.policy_of(kind, text_chars=120) == dp.TEMPLATE_POLICY


TEXT = "Factura FA-2024/0999 del 15/03/2024. Total 1.250,00 €. Springfield."


class TestVerifyCaps:
    def test_life_domains_are_capped_at_three(self):
        p, _ = dp.grade(DriveProfile(title="", what_it_is="", language="", doc_type="invoice",   # Δ59: was verify
                                     life_domains=("finance", "home", "work", "family")), _chunks(TEXT), "f.pdf")
        assert p.life_domains == ("finance", "home", "work")

    def test_a_value_keeps_its_maximal_form_not_a_fragment(self):
        p, counts = dp.grade(DriveProfile(title="", what_it_is="", language="", doc_type="invoice", key_values=(
            KeyValue("invoice_number", "FA-2024/0999", "2024"),       # the model's canonical is a fragment
            KeyValue("amount_due", "1.250,50 €", "1250.50"),
            KeyValue("amount_total", "1.250,00 €", "1250.00"))), _chunks(TEXT), "f.pdf")
        # Δ59: the amount the text lacks is kept with the model's canonical, graded not_in_text (was dropped)
        assert [kv.canonical for kv in p.key_values] == ["fa20240999", "1250.50", "1250.00"]
        assert [(g.value, g.grade) for g in p.graded if g.field == "value"] == [
            ("FA-2024/0999", "in_text"), ("1.250,50 €", "not_in_text"), ("1.250,00 €", "in_text")]
        assert counts.not_in_text == {"value": 1}
