"""Search v2 cross-cutting review fixes on the keyer and the tiers (USER_DRIVE_INDEXING_RFC §25.2.4, §25.5.1, §25.5.4,
D31; reviews xc_sv2_smart F1-F3 and the P7 eval items). New behaviour, new tests."""
import pytest

from src.domain.drive_enrichment import DriveProfile, ProfileEntity
from src.domain.drive_index import KEYS_VERSION
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import (
    GENERIC_WORDS, about_keys_of, name_keys_v2, name_word_concepts, query_concepts, word_keys,
)
from src.domain.drive_labels import DOC_TYPE_NAMES, DOC_TYPES, LIFE_DOMAIN_NAMES, LIFE_DOMAINS, label_names_digest
from src.domain.drive_match import Concept, SourceClass
from src.domain.drive_profile import verify
from src.domain.drive_tier_explain import explain
from src.domain.drive_tiers import FileKeys, allowances, classify, record_cover


class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


def _concept(query: str) -> Concept:
    (c,) = query_concepts(query).concepts
    return c


def _record(entity: ProfileEntity, **profile) -> RecordKeyInputs:
    return RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "en", profile.pop("doc_type", "letter"),
                                                           entities=(entity,), **profile))


# -- F1: only spellings the file prints are exact ------------------------------------------------------------------

def test_verification_records_which_model_spellings_the_file_prints():
    text = [_Chunk("Boarding pass. Destination: BARCELONA (BCN). Gate 12.")]
    entity = ProfileEntity(name="Барселона", type="place", printed="BARCELONA", forms=("Barcelona", "Барселоне"))
    profile, _ = verify(DriveProfile("t", "w", "en", "ticket", entities=(entity,)), text, "pass.pdf")
    (kept,) = profile.entities
    assert kept.found == ("Barcelona",)          # case-insensitive occurrence; the Cyrillic spellings are not printed


def test_a_hallucinated_form_is_never_an_exact_key():
    """The reviewer's probe: a letter about a fence whose profile wrote "паспорт" as a form of "Smith"."""
    entity = ProfileEntity(name="паспорт", type="person", printed="Smith", forms=("паспорт", "passport"))
    keys = set(about_keys_of(_record(entity)))
    assert "e:smith" in keys
    assert not {k for k in keys if k.startswith("e:") and ("паспорт" in k or "passport" in k)}
    assert {"ef:паспорт", "ef:passport"} <= keys
    cover = record_cover(FileKeys("x", about_keys=frozenset(keys)), _concept("паспорт"))
    assert cover is not None and cover.source_class is SourceClass.TRANSLATION


def test_a_found_spelling_stays_exact():
    entity = ProfileEntity(name="Barcelona", type="place", printed="BARCELONA", forms=("Барселона",),
                           found=("Barcelona",))
    keys = frozenset(about_keys_of(_record(entity)))
    latin = record_cover(FileKeys("x", about_keys=keys), _concept("barcelona"))
    cyrillic = record_cover(FileKeys("x", about_keys=keys), _concept("барселону"))
    assert latin is not None and latin.source_class is SourceClass.VERIFIED_RECORD
    assert cyrillic is not None and cyrillic.source_class is SourceClass.TRANSLATION   # ef: or the skeleton


def test_the_match_line_of_an_index_spelling_names_what_the_file_prints():
    entity = ProfileEntity(name="Барселона", type="place", printed="BARCELONA")
    rec = _record(entity)
    concept = _concept("барселона")
    match = classify(FileKeys("x", about_keys=frozenset(about_keys_of(rec))), [concept], allowances([concept],
                     {concept.word: 1}, 10), {}, {})
    assert match is not None
    (source,) = explain(match, rec, "pass.pdf", None, ()).sources
    assert source == 'place "Барселона" (spelling by the index; named in the file as "BARCELONA")'


def test_keys_version_is_bumped_for_the_review_fixes():
    assert KEYS_VERSION == 3


# -- F3: two Latin words never meet through a skeleton --------------------------------------------------------------

def test_a_latin_words_skeleton_is_its_own_family_in_chunks_names_and_records():
    assert any(k.startswith("xl:") for k in word_keys("A week in Barcelona."))
    assert not any(k.startswith("x:") for k in word_keys("A week in Barcelona."))
    assert any(k.startswith("x:") for k in word_keys("Неделя в Барселоне."))
    assert any(k.startswith("xl:") for k in name_keys_v2("Maria.jpg"))
    rec = _record(ProfileEntity(name="Le Havre", type="place", printed="Le Havre"))
    assert any(k.startswith("exl:") for k in about_keys_of(rec))
    assert not any(k.startswith("ex:") for k in about_keys_of(rec))


def test_a_latin_query_word_reads_only_cyrillic_skeletons():
    c = _concept("Mario")
    assert all(not k.startswith(("xl:", "exl:")) for k in (*c.word_keys, *c.about_keys))
    assert any(k.startswith("x:") for k in c.word_keys)


def test_a_cyrillic_query_word_reads_both_skeleton_families():
    c = _concept("Барселоне")
    assert any(k.startswith("x:") for k in c.word_keys) and any(k.startswith("xl:") for k in c.word_keys)
    assert any(k.startswith("exl:") for k in c.about_keys)


@pytest.mark.parametrize("text,query", [("Meeting with Maria.", "Mario"), ("La casa nueva.", "caso")])
def test_two_latin_words_never_meet_in_a_chunk(text, query):
    keys = set(word_keys(text))
    assert not keys & set(_concept(query).word_keys)


@pytest.mark.parametrize("text,query", [("Неделя в Барселоне.", "Barcelona"), ("A week in Barcelona.", "Барселону")])
def test_the_cross_script_skeleton_still_meets(text, query):
    assert set(word_keys(text)) & set(_concept(query).word_keys)


def test_a_cyrillic_spelling_match_within_one_script_is_labelled_as_another_spelling():
    file = FileKeys("x", name_keys=frozenset(name_keys_v2("Київ.jpg")))
    concept = _concept("киев")
    match = classify(file, [concept], allowances([concept], {concept.word: 1}, 10), {}, {})
    assert match is not None
    (source,) = explain(match, RecordKeyInputs(kind="image"), "Київ.jpg", None, ()).sources
    assert source == 'name "київ", by another spelling (automatic)'


def test_a_latin_query_over_a_cyrillic_name_is_labelled_across_scripts():
    file = FileKeys("x", name_keys=frozenset(name_keys_v2("Київ.jpg")))
    concept = _concept("kyiv")
    match = classify(file, [concept], allowances([concept], {concept.word: 1}, 10), {}, {})
    assert match is not None
    (source,) = explain(match, RecordKeyInputs(kind="image"), "Київ.jpg", None, ()).sources
    assert source.endswith(", by spelling in another script (automatic)")


# -- F2: a query of function or time words still finds the name ----------------------------------------------------

@pytest.mark.parametrize("word", ["año", "ano", "mañana", "manana"])
def test_time_words_are_removed_with_or_without_accents(word):
    q = query_concepts(word)
    assert q.concepts == () and q.ignored == (word,)


def test_name_word_concepts_prefer_the_generic_words():
    """"all my documents" searches "all" and "documents" by name, as before; "my" joins only when nothing generic was
    said (a strict-AND name search on "my" would empty the result)."""
    assert [c.word for c in name_word_concepts("all my documents")] == ["all", "documents"]
    assert [c.word for c in name_word_concepts("my")] == ["my"]
    assert [c.word for c in name_word_concepts("the the")] == ["the"]


def test_a_one_letter_word_is_never_a_name_word():
    assert name_word_concepts("a") == ()


def test_a_name_of_two_letter_function_words_only_keys_them_exactly():
    assert "w:my" in name_keys_v2("My.docx") and "w:ou" in name_keys_v2("Où.txt")
    assert "w:my" not in name_keys_v2("My budget.docx")   # a stem with a keyed word keys no function word


def test_a_latin_name_word_reads_no_latin_skeleton():
    (c,) = name_word_concepts("today")
    assert not any(k.startswith("xl:") for k in c.word_keys)


# -- P7 eval: interrogatives are generic; numbers are never the dropped word -----------------------------------------

@pytest.mark.parametrize("word", ["what", "which", "much", "many", "сколько", "скільки", "какой", "какие", "який",
                                  "які", "cuánto", "cuanto", "cuántas", "qué"])
def test_interrogative_phrasing_words_are_generic(word):
    assert word in GENERIC_WORDS or word.replace("á", "a") in GENERIC_WORDS
    assert word in query_concepts(f"{word} electricity").ignored


def test_a_year_is_never_the_low_weight_word():
    q = query_concepts("electricity bill 2024")
    by_word = {c.word: c for c in q.concepts}
    assert by_word["2024"].is_number
    df = {"2024": 40, "electricity": 3, "bill": 5}   # the year is the most frequent: it would be the lowest weight
    allowance = allowances(q.concepts, df, 100)
    assert allowance.low_weight == "bill"


def test_with_only_numbers_beside_one_word_nothing_is_missable():
    q = query_concepts("bill 2024 2025")
    allowance = allowances(q.concepts, {c.word: 10 for c in q.concepts}, 100)
    assert allowance.low_weight == "bill"   # the only non-number word: numbers are never chosen


# -- D31: deterministic names of the fixed labels ------------------------------------------------------------------

def test_every_label_value_has_a_names_row():
    assert set(DOC_TYPE_NAMES) == set(DOC_TYPES) and set(LIFE_DOMAIN_NAMES) == set(LIFE_DOMAINS)
    assert all(DOC_TYPE_NAMES[t] for t in DOC_TYPES if t != "other")
    assert all(LIFE_DOMAIN_NAMES[d] for d in LIFE_DOMAINS if d != "other")


def test_the_label_names_are_pinned():
    assert label_names_digest() == "d3c64502bc094ab5f7e73e25a62c18ad57d0b5a2158baddc6a7196a98c9b3ecd", (
        "the D31 tables changed: bump KEYS_VERSION and pin the new digest")


def test_the_doc_types_names_are_label_word_keys_without_any_model_label_word():
    rec = RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "es", "medical_record",
                                                           life_domains=("health",), topic_words=("холестерин",)))
    file = FileKeys("x", about_keys=frozenset(about_keys_of(rec)))
    q = query_concepts("медицинские записи по холестерину")
    covers = [record_cover(file, c) for c in q.concepts]
    assert all(c is not None and c.source_class is SourceClass.AUTOMATIC_RECORD for c in covers)
    match = classify(file, list(q.concepts), allowances(q.concepts, {c.word: 1 for c in q.concepts}, 50), {}, {})
    assert match is not None and match.tier.value == "labelled"


def test_the_life_areas_names_are_domain_word_keys():
    rec = RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "en", "invoice", life_domains=("vehicle",)))
    keys = set(about_keys_of(rec))
    assert {"dw:coche", "dw:машина", "lw:factura", "lw:invoice"} <= keys


def test_the_model_label_words_come_before_the_table():
    rec = RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "en", "ticket", label_words=("pase",)))
    keys = [k for k in about_keys_of(rec) if k.startswith("lw:")]
    assert keys[0] == "lw:pase" and "lw:квиток" in keys


def test_no_table_names_for_other():
    rec = RecordKeyInputs(kind="pdf", profile=DriveProfile("t", "w", "en", "other", life_domains=("other",)))
    assert not [k for k in about_keys_of(rec) if k.startswith(("lw:", "dw:"))]


def test_an_entity_without_found_spellings_reads_back_from_its_printed_form_only():
    """A profile stored before `found` existed keys only `printed` exactly."""
    entity = ProfileEntity(name="Acme", type="organization", printed="ACME Rentals")
    keys = set(about_keys_of(_record(entity)))
    assert "ef:acme" in keys and "e:acme" not in keys and "e:acm_rental" in keys
