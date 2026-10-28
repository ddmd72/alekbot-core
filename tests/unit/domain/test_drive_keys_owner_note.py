"""The owner note's keys (DRIVE_FILE_NOTES_RFC §5.4, D6, D7; review I5, I6): existing families under
`AboutField.OWNER_NOTE` (no new family: no KEYS_VERSION bump), emitted first, classifiers then numbers and dates then
words, the note's own cap cutting only the last words; the owner's classifiers override the profile's grouping keys,
also on a record with no profile; a fragment of a value never keys. The existing pins (`index_tables_digest`,
`golden_digest`, capture and scene digests) stay in tests/unit/domain/test_drive_keys_tables.py and must pass unchanged.
Synthetic notes only."""
from __future__ import annotations

from datetime import date
from itertools import combinations

import pytest

from src.domain.drive_enrichment import DriveProfile, KeyValue, ProfileDate, ProfileEntity
from src.domain.drive_index_params import ABOUT_KEYS_PER_RECORD, NOTE_KEYS_MAX
from src.domain.drive_key_sources import AboutField, RecordKeyInputs, effective_doc_type, effective_life_areas
from src.domain.drive_keys import (
    about_keys_capped, about_keys_of, about_sources, note_keys, note_keys_digest, query_concepts,
    source_class_of_about_key,
)
from src.domain.drive_labels import DOC_TYPES, LIFE_DOMAINS
from src.domain.drive_match import SourceClass
from src.domain.drive_note import (
    NOTE_CHARS_MAX, NOTE_KEYS_VERSION, NOTE_WORDS_MAX, check_note_text, note_word_count,
)
from src.domain.drive_note_merge import NOTE_MERGE_VERSION

# 80 noun-dense Cyrillic words within 600 characters: with the worst doc type and three areas, one word past the cap
MAXIMAL_RU = ("окна пены клеи биты ножи тазы баки полы ниши арки рамы замки обоев трубы краны плиты шкафы петли ручки "
              "лампы листы рейки двери дрели диски ключи вёдра кисти доски балки опоры сетки ленты скобы шайбы гайки "
              "болты винты муфты котлы стены плитки краски фасады кабели дюбели уголки панели пороги откосы отливы "
              "уровни свёрла лопаты валики брусья стойки плёнки гвозди хомуты насосы перила люстры розетки провода "
              "коробки профили рулетки молотки шпатели фитинги вентили фильтры потолки колонны ступени радиаторы "
              "стяжки кирпичи штукатурки")
WORST_TYPE = max(DOC_TYPES, key=lambda t: len(note_keys("", t)))
WORST_AREAS = max(combinations(LIFE_DOMAINS, 3), key=lambda a: len(note_keys("", "", a)))


def _covers(query: str, keys) -> bool:
    """Every concept of `query` meets one of `keys` (strict AND over the about stream)."""
    have = set(keys)
    return all(have & set(c.about_keys) for c in query_concepts(query).concepts)


def test_the_maximal_note_is_a_valid_note():
    assert note_word_count(MAXIMAL_RU) == NOTE_WORDS_MAX and len(MAXIMAL_RU) <= NOTE_CHARS_MAX
    assert check_note_text(MAXIMAL_RU) == MAXIMAL_RU


def test_note_keys_use_existing_families_only():
    keys = note_keys("the lock invoice FA-0137, 249,00 EUR, 15.09.2026, September 2026", "contract", ("home",))
    assert {k.split(":", 1)[0] + ":" for k in keys} <= {"e:", "ex:", "exl:", "v:", "y:", "m:"}


def test_order_classifiers_then_numbers_and_dates_then_words():
    keys = note_keys("Ремонт ванної 1.048,45 EUR", "contract", ("home",))
    first_number = next(i for i, k in enumerate(keys) if k.startswith("v:"))
    first_word = keys.index("e:ремонт")
    classifiers = note_keys("", "contract", ("home",))
    assert keys[:len(classifiers)] == classifiers
    assert len(classifiers) <= first_number < first_word


def test_classifier_names_carry_no_skeletons():
    assert all(k.startswith("e:") for k in note_keys("", "contract", ("home", "finance")))


def test_the_worst_type_and_three_areas_stay_under_one_hundred_keys():
    worst = len(note_keys("", WORST_TYPE, WORST_AREAS))
    assert worst < 100
    for doc_type in DOC_TYPES:
        for areas in combinations(LIFE_DOMAINS, 3):
            assert len(note_keys("", doc_type, areas)) <= worst


def test_a_maximal_note_keeps_every_classifier_key_and_cuts_only_trailing_words():
    keys = note_keys(MAXIMAL_RU, WORST_TYPE, WORST_AREAS)
    assert len(keys) == NOTE_KEYS_MAX
    assert set(note_keys("", WORST_TYPE, WORST_AREAS)) <= set(keys)
    assert not set(note_keys("штукатурки")) & set(keys)          # the last word is cut
    assert set(note_keys("кирпичи")) <= set(keys)                # the one before it is kept
    rec = RecordKeyInputs(kind="pdf", owner_note_text=MAXIMAL_RU, owner_doc_type=WORST_TYPE,
                          owner_life_areas=WORST_AREAS)
    assert about_keys_capped(rec)[1] is True                     # the card says "keys cut"


def test_no_fragment_value_key():
    keys = note_keys("the Porto flat rent is 1 250,00 EUR")
    assert {"v:1250.00", "v:1250"} <= set(keys) and "v:250" not in keys
    assert not _covers("250", keys)
    assert _covers("1250", keys) and _covers("1 250,00", keys)


def test_a_date_keys_its_iso_value_year_and_month():
    keys = note_keys("received on 28.10.2026")
    assert {"v:2026-10-28", "y:2026", "m:2026-10"} <= set(keys)
    assert _covers("28.10.2026", keys) and _covers("2026", keys)


def test_a_month_year_and_a_standalone_year():
    assert "m:2026-10" in note_keys("the decision of October 2026")
    assert _covers("October 2026", note_keys("the decision of October 2026"))
    assert {"y:2026", "v:2026"} <= set(note_keys("submitted in 2026"))
    assert "y:1850" not in note_keys("built in 1850")


def test_sources_and_classes_of_a_noted_record():
    rec = RecordKeyInputs(kind="pdf", owner_note_text="Рахунок підрядника", owner_doc_type="contract")
    sources = about_sources(rec)
    assert all(s.field is AboutField.OWNER_NOTE for s in sources["e:рахунок"])
    assert source_class_of_about_key("e:рахунок") is SourceClass.VERIFIED_RECORD
    skeleton = next(k for k in about_keys_of(rec) if k.startswith("ex:"))
    assert source_class_of_about_key(skeleton) is SourceClass.TRANSLATION
    assert {s.detail for s in sources["e:договор"]} == {"type"}


def test_note_keys_lead_the_about_keys():
    rec = RecordKeyInputs(kind="pdf", owner_note_text="Рахунок підрядника", owner_doc_type="contract",
                          profile=DriveProfile("t", "w", "ru", "invoice"))
    keys = about_keys_of(rec)
    note = note_keys("Рахунок підрядника", "contract")
    assert keys[:len(note)] == note


def _rich_profile() -> DriveProfile:
    words = tuple(f"слово{i}ка" for i in range(30))
    return DriveProfile(
        "Title", "What", "ru", "invoice", life_domains=("finance", "home", "work"),
        dates=tuple(ProfileDate("due", date(2024, m, 1)) for m in range(1, 7)),
        entities=tuple(ProfileEntity(f"Компания{i} Иванов{i}", "person", f"Компания{i} Иванов{i}",
                                     tuple(f"форма{i}{j}" for j in range(8))) for i in range(12)),
        key_values=tuple(KeyValue(f"label{i}", f"INV-{i:04d}", f"inv{i:04d}") for i in range(12)),
        topic_words=words, label_words=words, domain_words=words)


def test_a_full_profile_and_a_full_note_never_cut_the_note_keys():
    rec = RecordKeyInputs(kind="pdf", owner_note_text=MAXIMAL_RU, owner_doc_type="contract",
                          owner_life_areas=("home",), profile=_rich_profile())
    keys, capped = about_keys_capped(rec)
    assert capped and len(keys) == ABOUT_KEYS_PER_RECORD
    assert set(note_keys(MAXIMAL_RU, "contract", ("home",))) <= set(keys)


def test_owner_classifiers_override_the_grouping_keys():
    rec = RecordKeyInputs(kind="pdf", owner_doc_type="contract", owner_life_areas=("home",),
                          profile=DriveProfile("t", "w", "en", "invoice", life_domains=("finance",)))
    keys = about_keys_of(rec)
    assert "l:type:contract" in keys and "l:type:invoice" not in keys
    assert "l:domain:home" in keys and "l:domain:finance" not in keys
    assert (effective_doc_type(rec), effective_life_areas(rec)) == ("contract", ("home",))


def test_the_owner_type_groups_a_record_with_no_profile():
    rec = RecordKeyInputs(kind="pdf", owner_doc_type="contract", owner_life_areas=("home",))
    assert {"l:type:contract", "l:domain:home"} <= set(about_keys_of(rec))
    assert not any(k.startswith("l:") for k in about_keys_of(RecordKeyInputs(kind="pdf")))


def test_without_owner_classifiers_the_profile_groups_as_before():
    rec = RecordKeyInputs(kind="pdf", owner_note_text="from the contractor",
                          profile=DriveProfile("t", "w", "en", "invoice", life_domains=("finance",)))
    assert {"l:type:invoice", "l:domain:finance"} <= set(about_keys_of(rec))
    assert (effective_doc_type(rec), effective_life_areas(rec)) == ("invoice", ("finance",))


def test_d31_names_are_dropped_only_for_an_overridden_field():
    profile = DriveProfile("t", "w", "en", "invoice", life_domains=("finance",), label_words=("bill",))
    base = about_keys_of(RecordKeyInputs(kind="pdf", profile=profile))
    assert "lw:invoice" in base and "dw:finance" in base
    typed = about_keys_of(RecordKeyInputs(kind="pdf", profile=profile, owner_doc_type="contract"))
    assert "lw:invoice" not in typed and "lw:bill" in typed and "dw:finance" in typed   # the model's own words stay
    areas = about_keys_of(RecordKeyInputs(kind="pdf", profile=profile, owner_life_areas=("home",)))
    assert "dw:finance" not in areas and "lw:invoice" in areas


@pytest.mark.parametrize("query", ["contract", "договор", "contrato", "договір"])
def test_the_query_finds_the_owner_type(query):
    rec = RecordKeyInputs(kind="pdf", owner_doc_type="contract",
                          profile=DriveProfile("t", "w", "en", "invoice"))
    keys = about_keys_of(rec)
    assert _covers(query, keys)
    hits = [k for c in query_concepts(query).concepts for k in c.about_keys if k in keys]
    assert all(source_class_of_about_key(k) is SourceClass.VERIFIED_RECORD for k in hits)


def test_a_record_without_a_note_emits_no_note_keys():
    profile = DriveProfile("t", "w", "en", "invoice", life_domains=("finance",))
    rec = RecordKeyInputs(kind="pdf", profile=profile)
    assert not any(s.field is AboutField.OWNER_NOTE for ss in about_sources(rec).values() for s in ss)


# The note keyer's pin (§5.4): a change to its rules or fixture fails here until NOTE_MERGE_VERSION (the record copies)
# and NOTE_KEYS_VERSION (the note store's `keys`) move with it (review Minor 5).
NOTE_KEYS_DIGESTS = {(1, 1): "b4097763d420"}


def test_note_keys_digest_is_pinned_per_merge_and_keys_version():
    assert note_keys_digest()[:12] == NOTE_KEYS_DIGESTS[(NOTE_MERGE_VERSION, NOTE_KEYS_VERSION)]
