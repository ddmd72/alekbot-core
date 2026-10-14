"""The v1 name-key tests' user-visible properties, ported to Search v2 (USER_DRIVE_INDEXING_RFC §25.2.4 "Name keys",
§25.5.1, §25.5.3-§25.5.4): a file is found BY NAME for a query when every concept of `query_concepts(query)` (or, for a
query of generic words only, `name_word_concepts`) is covered by the record's `name_keys_v2` through
`name_query_keys(concept)` — exactly what `DriveSearchService._name_hits`/`_add_names` and `drive_tiers.record_cover`
do. The cover's class decides the tier: `w:`/`n:`/`y:` are exact (About), an `x:` skeleton is a translation (By
translation, P1 review M8). A name that does not cover the query but passes `name_match_v2` over the concepts' whole
keys and trigrams (service line 436) is a "similar name" (Related).

Deliberate v1 -> v2 changes, ported as the v2 property (not a loss):
- a name matched through another script or the other transliteration scheme ("kyiv" -> "Київ.jpg") is found, but as a
  translation, not exact (B2/M8);
- a Cyrillic stress mark is dropped from the token ("ко́т" is "кот"), v1 kept it;
- the Ukrainian apostrophe joins its word ("Пам'ять" is one word, P1 M4): the name is found typed with any apostrophe
  or none, no longer as two words "пам ять".

Properties v2 LOSES are kept as plain failing tests whose assertion message starts with "LOST IN V2" and names the
finding in the cross-cutting review (xc_sv2_smart.md). They are not xfail: the owner decides each one.
"""
from typing import Optional, Tuple

import pytest

from src.domain.drive_keys import name_keys_v2, name_match_v2, query_concepts
from src.domain.drive_match import Concept, SourceClass
from src.domain.drive_tier_pool import concept_trigrams, name_word_concepts
from src.domain.drive_tiers import CLASS_RANK, FileKeys, name_query_keys, record_cover

EXACT = SourceClass.VERIFIED_RECORD
TRANSLATION = SourceClass.TRANSLATION


def _concepts(query: str) -> Tuple[Concept, ...]:
    return query_concepts(query).concepts or name_word_concepts(query)


def name_cover(name: str, query: str) -> Optional[SourceClass]:
    """The weakest class the name needs to cover every concept of the query (its tier), or None when a concept is not
    covered or the query has no concept."""
    concepts = _concepts(query)
    if not concepts:
        return None
    file = FileKeys(item_id="x", name_keys=frozenset(name_keys_v2(name)))
    covers = [record_cover(file, c) for c in concepts]
    if any(c is None for c in covers):
        return None
    return max((c.source_class for c in covers if c is not None), key=lambda cls: CLASS_RANK[cls])


def similar(name: str, query: str) -> bool:
    """Over the `name_match_v2` floor on the query's whole name keys and trigrams (the Related "similar name")."""
    keys = tuple(dict.fromkeys(k for c in _concepts(query) for k in (*sorted(name_query_keys(c)), *concept_trigrams(c))))
    return name_match_v2(name_keys_v2(name), keys) is not None


def found(name: str, query: str) -> bool:
    return name_cover(name, query) is not None


# -- from test_drive_name_key_latin_folds.py ------------------------------------------------------------------------

@pytest.mark.parametrize("name,query", [
    ("İzmir.jpg", "izmir"),            # casefold("İ") = "i" + U+0307
    ("İSTANBUL 2024.jpg", "istanbul"),
    ("Đorđević.pdf", "dordevic"),      # đ
    ("Guðrún.txt", "gudrun"),          # ð
    ("Þingvellir.jpg", "thingvellir"), # þ
    ("Iğdır.jpg", "igdir"),            # ı (and ğ through NFKD)
    ("Ħamrun.jpg", "hamrun"),          # ħ
])
def test_latin_letters_are_found_unaccented_and_exact(name, query):
    assert name_cover(name, query) is EXACT


def test_a_cyrillic_stress_mark_is_dropped_so_the_plain_word_finds_it():
    """v2 property (v1 kept the mark): the token is "кот", found exactly by "кот", and no Latin letter folding is
    applied to a Cyrillic word."""
    assert name_cover("ко́т.txt", "кот") is EXACT
    assert "w:кот" in name_keys_v2("ко́т.txt")


# -- from test_drive_name_key_query_stopwords.py --------------------------------------------------------------------

@pytest.mark.parametrize("query,name", [
    ("the lease agreement", "Lease agreement.pdf"),
    ("photos from Rome", "Rome photos.zip"),
    ("my budget for 2026", "budget 2026.ods"),
    ("мой договор аренды", "Договор аренды.docx"),
    ("фото из Порту", "Порту фото.jpg"),
    ("ідеї для проєкту", "Ідеї проєкту.md"),
    ("план з бюджету", "План бюджету.xlsx"),
])
def test_a_name_with_every_content_word_covers_the_query_exactly(query, name):
    assert name_cover(name, query) is EXACT


@pytest.mark.parametrize("query,stop", [
    ("the lease of my flat", {"the", "of", "my"}),
    ("фото из Рима", {"из"}),
    ("файли з архіву", {"з"}),
])
def test_function_words_are_no_concept_and_take_no_name_key(query, stop):
    q = query_concepts(query)
    assert not {c.word for c in q.concepts} & stop and q.concepts
    keys = {k for c in q.concepts for k in name_query_keys(c)}
    assert not keys & {f"w:{w}" for w in stop}


def test_record_keys_keep_function_words_of_three_letters_or_more():
    """v2 property: a 1-2 letter function word is not keyed on the index (P1 review M2); a longer one is."""
    assert "w:the" in name_keys_v2("The lease.pdf")
    assert "w:из" not in name_keys_v2("фото из Рима.jpg")


@pytest.mark.parametrize("name,query", [
    ("The The - live.mp3", "the the"),   # a band
    ("My.docx", "my"),                   # a folder or file named by a function word
    ("Où.txt", "où"),                    # French "where"
    ("Maïs.txt", "maïs"),                # French "corn"; "mais" is the function word "but"
    ("Mañana.docx", "mañana"),           # a time word (TIME_WORDS) is the whole query
    ("Año.pdf", "año"),
    ("Today.md", "today"),
])
def test_a_query_of_removed_words_only_still_finds_the_name(name, query):
    """v1 `test_a_query_of_function_words_only_keeps_them`: with nothing else to search for, the removed words are
    the name. v2 searches only GENERIC words by name (`name_word_concepts`, P6 m6); function and time words leave no
    concept, and the service answers with the newest-files listing that says "No name words or filters were given"."""
    assert found(name, query), (f"LOST IN V2 (xc_sv2_smart F2): {query!r} leaves no concept, so {name!r} is not "
                                f"searched by name and the result is a newest-files listing")


# -- from test_drive_name_key_ukrainian_initial_yi.py / _ukrainian_initials.py -------------------------------------

@pytest.mark.parametrize("name,national,russian_style", [
    ("Їжакевич.jpg", "yizhakevych", "yizhakevich"),
    ("Яготин.jpg", "yahotyn", "yagotin"),
    ("Юрій.jpg", "yurii", "yuriy"),
    ("Йосипівка.jpg", "yosypivka", "yosipivka"),
    ("Євпаторія.jpg", "yevpatoriia", "yevpatoriya"),
])
def test_both_latin_schemes_find_the_cyrillic_name_as_a_translation(name, national, russian_style):
    assert name_cover(name, national) is TRANSLATION
    assert name_cover(name, russian_style) is TRANSLATION


# -- from test_drive_name_keys.py -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,query", [
    ("IMG_4912.JPG", "IMG 4912"), ("IMG_4912.JPG", "4912"), ("IMG_4912.JPG", "img"),
    ("MyReport2026.docx", "report 2026"), ("MyReport2026.docx", "report"),
])
def test_separators_case_and_digit_boundaries_split_the_name(name, query):
    assert name_cover(name, query) is EXACT


def test_nfc_and_casefold():
    import unicodedata
    assert name_keys_v2(unicodedata.normalize("NFD", "Ідеї.md")) == name_keys_v2(unicodedata.normalize("NFC", "Ідеї.md"))
    assert name_cover("ІДЕЇ.md", "ідеї") is EXACT


@pytest.mark.parametrize("name,query", [
    ("Договор_ред3.docx", "dogovor"),     # a Cyrillic name, a Latin query
    ("dogovor.pdf", "договор"),           # a Latin name, a Cyrillic query
    ("Договор_ред3.docx", "dogovor red3"),
])
def test_another_script_finds_the_name_as_a_translation(name, query):
    assert name_cover(name, query) is TRANSLATION


@pytest.mark.parametrize("name,query,covers", [
    ("Договор_ред3.docx", "ред3", True),
    ("Договор_ред3.docx", "договор ред4", False),
    ("IMG_4912.jpg", "IMG 4912", True),
])
def test_cover_needs_every_concept(name, query, covers):
    assert found(name, query) is covers


@pytest.mark.parametrize("name,query,is_similar", [
    ("Contrato de alquiler 2026.pdf", "alquilr", True),   # typo: shares alq, lqu, qui, uil
    ("Contrato de alquiler 2026.pdf", "xyz", False),
    ("Budget.ods", "bud", False),                          # one trigram, no whole token
    ("Déclaration fiscale.pdf", "declaraton", True),       # unaccented spelling keeps trigrams for typos
])
def test_a_typo_is_a_similar_name_never_a_cover(name, query, is_similar):
    assert not found(name, query)
    assert similar(name, query) is is_similar


def test_a_whole_word_finds_the_name_exactly():
    assert name_cover("Contrato de alquiler 2026.pdf", "alquiler") is EXACT


# -- from test_drive_name_key_variants.py ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,query", [
    ("Résumé.pdf", "resume"), ("Naïve.txt", "naive"), ("Crème brûlée recipe.docx", "creme brulee"),
    ("São Paulo trip.xlsx", "sao paulo"), ("Resume.pdf", "résumé"),
])
def test_a_name_is_found_exactly_without_or_with_its_accents(name, query):
    assert name_cover(name, query) is EXACT


def test_yo_is_found_as_ye():
    assert name_cover("Ёлка 2025.jpg", "елка") is EXACT


def test_short_i_and_yi_keep_their_letter_in_exact_keys():
    """й and ї are letters, not accented и and і: a misspelling with и/і meets the name only as a translation (the
    skeleton), never exactly."""
    assert "w:мои" not in name_keys_v2("Мой отчёт.pdf")
    assert "w:киів" not in name_keys_v2("Київ.jpg")
    assert name_cover("Київ.jpg", "киів") is TRANSLATION


@pytest.mark.parametrize("name,query", [
    ("Київ.jpg", "kyiv"), ("Київ.jpg", "kiyiv"), ("Харків 2024.jpg", "kharkiv"), ("Гуляйполе.pdf", "huliaipole"),
    ("Запоріжжя.png", "zaporizhzhia"), ("Їжак.txt", "yizhak"), ("Згорани.doc", "zghorany"),
    ("Олексій звіт.docx", "oleksii zvit"), ("Kyiv 2023.jpg", "Київ"),
])
def test_ukrainian_national_and_russian_style_transliterations_find_the_name(name, query):
    assert name_cover(name, query) is TRANSLATION


@pytest.mark.parametrize("name,query", [
    ("Report—final’s.docx", "report final"), ("Report–final.docx", "report final"), ("O’Brien.pdf", "o brien"),
    ("“Quoted” name.txt", "quoted name"), ("‘Single’ quotes.txt", "single quotes"),
    ("„Deutsch“ notes.txt", "deutsch notes"), ("Sale 50%.pdf", "sale 50"),
])
def test_typographic_dashes_quotes_and_percent_separate_words(name, query):
    assert name_cover(name, query) is EXACT


@pytest.mark.parametrize("name", ["Пам’ять.md", "Памʼять.md", "Пам'ять.md"])
@pytest.mark.parametrize("query", ["памʼять", "пам'ять", "пам’ять", "память"])
def test_a_ukrainian_apostrophe_word_is_found_with_any_apostrophe_or_none(name, query):
    """v2 property (P1 M4): the apostrophe joins the word; v1 split it into "пам" + "ять"."""
    assert name_cover(name, query) is EXACT


def test_percent_is_not_part_of_the_number():
    assert name_cover("50%.pdf", "50") is EXACT


def test_ukrainian_spelling_adds_no_trigrams_so_unrelated_names_stay_below_the_floor():
    """v1: "Подогрев" shares one real trigram with "Догов"; a third script form of it would pass the floor. In v2 the
    skeleton's trigrams join the query (`concept_trigrams`, P6 m4) and must still not lift it."""
    assert not similar("Подогрев 001.txt", "Догов")
    assert similar("Договор аренды.docx", "Догов")


@pytest.mark.parametrize("name,query", [
    ("Mamá.jpg", "mama"), ("Café.txt", "cafe"), ("Martín.pdf", "martin"), ("Canción.mp3", "cancion"),
    ("Perú viaje.jpg", "peru viaje"), ("Pingüino.png", "pinguino"), ("Año nuevo.jpg", "ano nuevo"),
    ("Mañana.docx", "manana"),
    ("Voilà.txt", "voila"), ("Château.jpg", "chateau"), ("Été.jpg", "ete"), ("Mère.pdf", "mere"),
    ("Fête.png", "fete"), ("Noël.jpg", "noel"), ("Île de Ré.jpg", "ile de re"), ("Hôtel.pdf", "hotel"),
    ("Août.jpg", "aout"), ("Capharnaüm.mkv", "capharnaum"), ("L'Haÿ-les-Roses.jpg", "hay les roses"),
    ("Garçon.docx", "garcon"), ("Cœur.pdf", "coeur"), ("Œuvre complète.pdf", "oeuvre complete"),
    ("Lætitia.jpg", "laetitia"), ("Øresund.jpg", "oresund"), ("Łódź.jpg", "lodz"), ("Straße.pdf", "strasse"),
])
def test_spanish_and_french_names_are_found_unaccented(name, query):
    assert name_cover(name, query) is EXACT


@pytest.mark.parametrize("name,query", [("Maïs.txt", "mais"), ("Où.txt", "ou")])
def test_a_name_that_folds_onto_a_function_word_is_still_found(name, query):
    """v1 `test_spanish_and_french_names_are_found_unaccented` held these two: "Maïs" (corn) typed "mais", "Où" typed
    "ou". In v2 "mais" and "ou" are function words, so a query of that word alone is no concept."""
    assert found(name, query), f"LOST IN V2 (xc_sv2_smart F2): {query!r} is a function word, so {name!r} is unfindable"


@pytest.mark.parametrize("name,query", [("Cœur.pdf", "cœur"), ("Été.jpg", "été"), ("Año nuevo.jpg", "año nuevo")])
def test_the_accented_original_spelling_finds_the_name(name, query):
    assert name_cover(name, query) is EXACT


def test_a_cyrillic_name_gets_no_latin_spelling_under_w():
    for name in ("Мой отчёт", "Київ", "Ёжик йод їжа"):
        latin = {k for k in name_keys_v2(name) if k.startswith("w:") and k[2:].isascii()}
        assert latin == set()


@pytest.mark.parametrize("name,query", [
    ("¿Qué pasa?.txt", "que pasa"), ("¡Hola!.txt", "hola"), ("Notes…draft.md", "notes draft"),
    ("Col·lecció.pdf", "col leccio"), ("«Guerre» et paix.pdf", "guerre et paix"),
])
def test_spanish_and_typographic_punctuation_separate_words(name, query):
    assert name_cover(name, query) is EXACT


# -- from tests/unit/services/test_drive_search_accent_trigram_floor.py ---------------------------------------------

def test_one_shared_accented_trigram_stays_below_the_floor():
    assert not similar("Amédée 001.txt", "Médic")
    assert similar("Médico informe.docx", "Médic") or found("Médico informe.docx", "Médic")


# -- a v2 hazard the port surfaced -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name,query", [
    ("Maria.jpg", "Mario"), ("Carla.pdf", "Carlo"), ("Paola.pdf", "Paolo"), ("Banca.pdf", "banco"),
    ("caso.pdf", "casa"), ("Marta.jpg", "Marty"),
])
def test_two_latin_words_never_meet_through_the_cyrillic_skeleton(name, query):
    """Not a v1 test: v1 kept transliterations of Latin words but its covers were whole-token. In v2 a Latin word's
    skeleton is the skeleton of its Russian-style Cyrillic spelling's STEM (`skeleton_key` -> `primary`), so the
    Cyrillic case endings are stripped from Latin words: "Mario" and "Maria" both become `x:mari`, and Maria's photos
    are shown under "By translation" for "Mario" ("by spelling in another script", which is false: both are Latin)."""
    assert not found(name, query), (f"LOST IN V2 (xc_sv2_smart F3): {query!r} covers {name!r} as a translation "
                                    f"through the Cyrillic skeleton")

