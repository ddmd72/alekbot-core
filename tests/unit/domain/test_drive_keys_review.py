"""Search v2 keyer, P1 review fixes (USER_DRIVE_INDEXING_RFC §25.2.4, §25.5.1, §25.5.4 as revised): one test group
per finding (B1, B2, M1-M9, minors). Synthetic inputs only."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_enrichment import DriveProfile, KeyValue, ProfileEntity
from src.domain.drive_index import DriveIndexRecord, TriageVerdict
from src.domain.drive_index_params import CONCEPT_KEYS_MAX, WORD_KEYS_PER_CHUNK
from src.domain.drive_key_sources import RecordKeyInputs
from src.domain.drive_keys import (
    about_keys_of, fetch_batches, name_keys_v2, query_concepts, source_class_of_about_key, source_class_of_word_key,
    token_forms, token_spans, word_keys, word_keys_capped,
)
from src.domain.drive_match import SourceClass

from .drive_keys_corpus import CORPUS


def _profile(**kw) -> DriveProfile:
    base = dict(title="", what_it_is="", language="ru", doc_type="other")
    base.update(kw)
    return DriveProfile(**base)


def _covered(query: str, rec) -> dict:
    keys = set(about_keys_of(rec))
    return {c.word: bool(set(c.about_keys) & keys) for c in query_concepts(query).concepts}


def _shared(query: str, text: str) -> dict:
    keys = set(word_keys(text))
    return {c.word: set(c.word_keys) & keys for c in query_concepts(query).concepts}


def _exact(query: str, text: str) -> bool:
    """Every concept meets the text through a passage-word key (native form or number), not only a skeleton."""
    return all(any(source_class_of_word_key(k) is SourceClass.PASSAGE_WORDS for k in keys)
               for keys in _shared(query, text).values())


# -- B1: coverage never depends on the neighbouring words ---------------------------------------------------------

@pytest.mark.parametrize("query", ["автомобиля", "страховка автомобиля Skoda Fabia",
                                   "страховка автомобиля Skoda Fabia полис каско 2024 Порту"])
def test_b1_a_word_is_covered_whatever_its_neighbours(query):
    rec = RecordKeyInputs(kind="pdf", profile=_profile(topic_words=("автомобиль", "car")))
    assert _covered(query, rec)["автомобиля"]


def test_b1_every_native_form_reaches_every_record_family():
    for query in ("медицинские записи холестерин анализ крови", "харків"):
        for c in query_concepts(query).concepts:
            for form in token_forms(c.word):
                for fam in ("e:", "k:", "lw:", "tw:", "dw:", "lv:", "pw:", "pe:"):
                    assert fam + form in c.about_keys, (c.word, fam + form)


def test_b1_the_fetch_batches_cover_the_whole_list_in_order():
    (c,) = query_concepts("харків").concepts
    batches = fetch_batches(c.about_keys)
    assert len(c.about_keys) > CONCEPT_KEYS_MAX and all(len(b) <= CONCEPT_KEYS_MAX for b in batches)
    assert tuple(k for b in batches for k in b) == c.about_keys


# -- B2: ordinary ru/uk prose stays under the chunk cap ----------------------------------------------------------

@pytest.mark.parametrize("name", sorted(CORPUS))
def test_b2_no_2400_char_window_of_ru_uk_prose_hits_the_cap(name):
    text = CORPUS[name]
    assert len(text) >= 2400
    assert WORD_KEYS_PER_CHUNK == 1500
    for i in range(0, len(text) - 2399, 200):
        keys, capped = word_keys_capped(text[i:i + 2400])
        assert not capped and len(keys) < 1000, (name, i, len(keys))


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_b2_every_word_of_a_chunk_is_found_by_its_own_query(name):
    chunk = CORPUS[name][:2400]
    keys = set(word_keys(chunk))
    for token, start, end in token_spans(chunk):
        for c in query_concepts(chunk[start:end]).concepts:
            assert set(c.word_keys) & keys, (chunk[start:end], c.word)


def test_b2_the_index_keeps_one_skeleton_per_word():
    keys = word_keys("Договор аренды квартиры")
    assert sorted(k for k in keys if k.startswith("x:")) == ["x:arend", "x:dohovor", "x:kvartir"]


# -- M1: case does not split a word -------------------------------------------------------------------------------

@pytest.mark.parametrize("query,text", [("iphone", "Чек на iPhone 15 Pro"), ("paypal", "Payment via PayPal"),
                                        ("mcdonalds", "Receipt McDonalds"), ("onedrive", "OneDrive backup"),
                                        ("IPHONE", "iphone case"), ("PayPal", "paypal receipt")])
def test_m1_a_word_is_found_whatever_its_casing(query, text):
    assert _exact(query, text)


def test_m1_a_case_part_is_stored_but_never_stands_for_the_whole_word():
    assert _exact("phone", "Чек на iPhone")                 # the index also stores the parts
    assert not _shared("iPhone", "receipt phone case")["iphone"]


def test_m1_entity_and_name_keep_the_whole_word():
    rec = RecordKeyInputs(kind="pdf", profile=_profile(entities=(ProfileEntity("iPhone 15", "product", "iPhone 15"),)))
    assert all(_covered("iphone 15", rec).values())
    assert {"w:iphone", "w:phone"} <= set(name_keys_v2("iPhone_receipt.pdf"))


# -- M2: two-letter words are concepts -----------------------------------------------------------------------------

@pytest.mark.parametrize("query,words", [("my CV", ["cv"]), ("TV", ["tv"]), ("договор ИП", ["договор", "ип"]),
                                         ("ЖК Комфорт", ["жк", "комфорт"]), ("BMW X5", ["bmw", "x5"]),
                                         ("пароль Wi-Fi", ["пароль", "wifi"])])
def test_m2_two_letter_words_are_concepts(query, words):
    assert [c.word for c in query_concepts(query).concepts] == words


def test_m2_a_two_letter_word_has_its_exact_form_only():
    (c,) = query_concepts("ИП").concepts
    assert c.word_keys == ("w:ип",) and c.hint_keys == ("w:ип",)
    assert _exact("ИП", "Договор с ИП Мельник") and not _shared("ИП", "Договор с ООО Мельник")["ип"]
    assert "w:на" not in word_keys("на столе") and "w:ип" in word_keys("ИП Мельник")


# -- M3: values meet in the form the query produces ---------------------------------------------------------------

@pytest.mark.parametrize("canonical,query", [
    ("INV-2024/0012", "INV-2024/0012"), ("1.250,00", "1250.00"), ("XX00 1111 2222 3333", "xx00 1111 2222 3333"),
    ("+447 700 900 456", "447 700 900 456"), ("XKJQPL", "xkjqpl"),
])
def test_m3_a_verified_value_meets_its_query(canonical, query):
    rec = RecordKeyInputs(kind="pdf", profile=_profile(key_values=(KeyValue("reference", canonical, canonical),)))
    keys = set(about_keys_of(rec))
    (c,) = query_concepts(query).concepts
    hit = {k for k in set(c.about_keys) & keys if k.startswith("v:")}
    assert hit and all(source_class_of_about_key(k) is SourceClass.VERIFIED_RECORD for k in hit)


# -- M4: the Ukrainian apostrophe is part of the word -------------------------------------------------------------

@pytest.mark.parametrize("apostrophe", ["'", "’", "ʼ", "`", "ʻ"])
def test_m4_the_apostrophe_keeps_the_word_whole(apostrophe):
    assert [t for t, _, _ in token_spans(f"сім{apostrophe}я")] == ["сімʼя"]
    assert _exact("компютер", f"нового комп{apostrophe}ютера")
    assert _exact(f"комп{apostrophe}ютер", "компютера")


def test_m4_a_word_with_an_apostrophe_is_not_its_prefix():
    assert not _shared("сім'я", "сім днів")["сімʼя"]
    assert _exact("сім'я", "у сім'ї")
    assert [t for t, _, _ in token_spans("O'Brien l'hotel")] == ["o", "brien", "l", "hotel"]


# -- M5: adjective and Ukrainian endings ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,text", [("новые", "новая квартира"), ("нової квартири", "нова квартира"),
                                        ("медичної", "медична картка"), ("медицинские", "медицинская карта"),
                                        ("здание", "здания суда"), ("красными", "красная"), ("синіми", "синій")])
def test_m5_adjective_and_plural_endings_link(query, text):
    assert _exact(query, text)


# -- M6: Latin names reach Ukrainian locatives ---------------------------------------------------------------------

@pytest.mark.parametrize("query,text", [("Odesa", "Квартира в Одесі"), ("Kharkiv", "Поїздка в Харкові"),
                                        ("Одесі", "Apartment in Odesa"), ("Харкові", "Office in Kharkiv"),
                                        ("Odesa", "з Одеси"), ("Coimbra", "в Коїмбрі")])
def test_m6_latin_and_cyrillic_spellings_meet_through_the_skeleton(query, text):
    hits = list(_shared(query, text).values())[0]
    assert hits and all(source_class_of_word_key(k) is SourceClass.TRANSLATION for k in hits)


# -- M7: photo and image words -------------------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["фотографии", "фотографий", "фотографії", "фотки", "снимки", "знімки", "photographs",
                                  "изображения", "зображення", "картинки", "fotografías", "photo", "фото"])
def test_m7_photo_words_cover_a_photo(word):
    photo = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO)
    assert all(_covered(word, photo).values()), word


# -- M8: a match across scripts is a translation, never exact ------------------------------------------------------

@pytest.mark.parametrize("query,text", [("hotel Porto", "Я хотел поехать в Порту"), ("cost", "Перелом кости"),
                                        ("net", "нет дохода"), ("list", "больничный лист"), ("data", "дата подписи")])
def test_m8_transliteration_false_links_are_translations_only(query, text):
    for word, hits in _shared(query, text).items():
        assert all(source_class_of_word_key(k) is SourceClass.TRANSLATION for k in hits), (word, hits)
    assert not _exact(query, text)


def test_m8_a_cross_script_entity_is_a_translation_and_a_stored_spelling_exact():
    rec = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, place_forms=("Coimbra",))
    (c,) = query_concepts("Коїмбрі").concepts
    hits = set(c.about_keys) & set(about_keys_of(rec))
    assert hits and {source_class_of_about_key(k) for k in hits} == {SourceClass.TRANSLATION}
    rec2 = RecordKeyInputs(kind="image", triage_verdict=TriageVerdict.PHOTO, place_forms=("Coimbra", "Коїмбра"))
    hits = set(c.about_keys) & set(about_keys_of(rec2))
    assert SourceClass.VERIFIED_RECORD in {source_class_of_about_key(k) for k in hits}


# -- the record type itself is keyed (the core now carries every §25.2.1 field) ------------------------------------

def test_a_drive_index_record_is_keyed_directly():
    rec = DriveIndexRecord(user_id="u", connection_id="u", item_id="i", kind="image", triage_verdict=TriageVerdict.PHOTO,
                           effective_date=datetime(2023, 5, 1, tzinfo=timezone.utc), place_forms=("Berlin",))
    assert {"e:berlin", "y:2023", "k:photo"} <= set(about_keys_of(rec))


# -- minors ---------------------------------------------------------------------------------------------------------

def test_m1_minor_a_short_letter_digit_id_is_its_words():
    assert [c.word for c in query_concepts("ред3").concepts] == ["ред", "3"]
    assert _exact("ред3", "Договор, ред. 3") and _exact("ред3", "Договор ред3")


@pytest.mark.parametrize("text", ["в марте 2023", "у березні 2023", "March 2023", "15 марта 2023"])
def test_m2_minor_month_locatives(text):
    (c,) = query_concepts("March 2023").concepts
    assert set(c.word_keys) & set(word_keys(f"Отчёт {text} года"))


def test_m3_minor_audio_kind_does_not_cover_every_record_word():
    audio = RecordKeyInputs(kind="audio")
    assert not _covered("записи", audio)["записи"] and _covered("аудиозапись", audio)["аудиозапись"]


def test_m4_minor_todo_is_generic_by_the_rfc_como_is_a_word():
    q = query_concepts("todo list")
    assert [c.word for c in q.concepts] == ["list"] and q.ignored == ("todo",)
    assert [c.word for c in query_concepts("Lake Como").concepts] == ["lake", "como"]


@pytest.mark.parametrize("query,text", [("email", "send an e-mail"), ("e-mail", "send email"), ("Wi-Fi", "WiFi router"),
                                        ("Wi-Fi", "wi-fi пароль")])
def test_m5_minor_hyphen_compounds_meet_their_joined_form(query, text):
    assert _exact(query, text)


@pytest.mark.parametrize("text", ["квар­тира у центрі", "квар-\nтира у центрі", "квар-\n  тира у центрі"])
def test_m6_minor_extraction_artefacts_do_not_split_words(text):
    assert _exact("квартира", text)


def test_m6_minor_stress_marks_are_dropped():
    assert _exact("замок", "старый за́мок") and _exact("за́мок", "замок")


def test_m7_minor_yo_folds_to_ye():
    assert _exact("ёлка", "елка") and _exact("елка", "ёлка") and _exact("счет", "счёт")


def test_m8_minor_a_person_is_about_their_surname():
    rec = RecordKeyInputs(kind="pdf", profile=_profile(entities=(ProfileEntity("Тарас Мельник", "person",
                                                                              "Тарас Мельник"),)))
    (c,) = query_concepts("Мельнику").concepts
    hits = set(c.about_keys) & set(about_keys_of(rec))
    assert any(source_class_of_about_key(k) is SourceClass.VERIFIED_RECORD for k in hits)


# m10: ordinary stemming links that are known and accepted (pinned so a table change that widens them shows)
KNOWN_FALSE_LINKS = [("почта", "почти"), ("банка", "банк")]
MUST_NOT_LINK = [("берлога", "берлін"), ("ганна", "ганновер"), ("сім'я", "сім"), ("iphone", "phone"),
                 ("ип", "ипотека"), ("квиток", "квитанция")]


@pytest.mark.parametrize("a,b", KNOWN_FALSE_LINKS)
def test_m10_minor_known_false_links_stay_known(a, b):
    assert _exact(a, b)


@pytest.mark.parametrize("a,b", MUST_NOT_LINK)
def test_m10_minor_negative_pins(a, b):
    assert not _exact(a, b) and not _exact(b, a)
