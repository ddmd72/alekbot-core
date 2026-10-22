"""The owner's note in a card's `matched:` line (DRIVE_FILE_NOTES_RFC §6.1): `your note "<word>"`, `your note: type
"<name>"`, `your note: life area "<name>"`; a note-only match is About (exact: the owner's own words); a match across
scripts through the note's skeleton is By translation. Synthetic records only."""
from __future__ import annotations

from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import about_keys_of, name_keys_v2, query_concepts
from src.domain.drive_match import MatchTier
from src.domain.drive_tier_explain import explain
from src.domain.drive_tiers import Allowance, FileKeys, classify


def _match(rec, name, query):
    f = FileKeys("f", frozenset(about_keys_of(rec)), frozenset(name_keys_v2(name)), {}, {})
    m = classify(f, query_concepts(query).concepts, Allowance(), {}, {})
    return m, explain(m, rec, name, None, ())


NOTED = RecordKeyInputs(kind="pdf", owner_note_text="Смета Пети на ремонт кухни", owner_doc_type="contract",
                        owner_life_areas=("home",), profile=DriveProfile("t", "w", "ru", "invoice"))


def test_a_note_word_match_is_about_and_says_your_note():
    m, ex = _match(NOTED, "scan.pdf", "смета")
    assert m.tier is MatchTier.ABOUT
    assert ex.sources == ('your note "Смета"',)


def test_the_owner_type_and_area():
    _, ex = _match(NOTED, "scan.pdf", "договор")
    assert ex.sources == ('your note: type "договор"',)
    _, ex = _match(NOTED, "scan.pdf", "житло")
    assert ex.sources == ('your note: life area "житло"',)


def test_a_note_only_match_of_several_words_is_about():
    m, ex = _match(NOTED, "scan.pdf", "смета кухни")
    assert m.tier is MatchTier.ABOUT
    assert ex.sources == ('your note "Смета"', 'your note "кухни"')


def test_a_match_across_scripts_is_by_translation():
    m, ex = _match(NOTED, "scan.pdf", "smeta")
    assert m is not None and m.tier is MatchTier.TRANSLATED
    assert ex.sources[0].startswith('your note "Смета"')
