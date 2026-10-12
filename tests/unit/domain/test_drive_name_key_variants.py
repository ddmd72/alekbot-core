"""Name keys match a name typed without its accents, in the Ukrainian national transliteration, and across
typographic punctuation (USER_DRIVE_INDEXING_RFC §3.2, §10.1 step 3a). Record and query keys come from the same
function, so each case is checked both ways where the direction matters."""
import pytest

from src.domain.drive_index_params import NAME_QUERY_KEYS_MAX
from src.domain.drive_name_keys import covers_query, name_keys, name_match, query_name_keys, query_token_groups


def _finds(name: str, query: str) -> bool:
    record = name_keys(name)
    return name_match(record, query_name_keys(query)) is not None and covers_query(record, query_token_groups(query))


@pytest.mark.parametrize("name,query", [
    ("Résumé.pdf", "resume"),
    ("Naïve.txt", "naive"),
    ("Crème brûlée recipe.docx", "creme brulee"),
    ("São Paulo trip.xlsx", "sao paulo"),
])
def test_a_name_is_found_without_its_accents(name, query):
    assert _finds(name, query)


def test_an_accented_query_finds_a_plain_name():
    assert _finds("Resume.pdf", "résumé")


def test_yo_is_found_as_ye():
    assert _finds("Ёлка 2025.jpg", "елка")


def test_short_i_and_yi_keep_their_letter():
    # й and ї are letters, not accented и and і: no stripped variant is made of them.
    assert "w:мои" not in name_keys("Мой отчёт.pdf")
    assert "w:киів" not in name_keys("Київ.jpg")


@pytest.mark.parametrize("name,query", [
    ("Київ.jpg", "kyiv"),
    ("Харків 2024.jpg", "kharkiv"),
    ("Гуляйполе.pdf", "huliaipole"),
    ("Запоріжжя.png", "zaporizhzhia"),
    ("Їжак.txt", "yizhak"),
    ("Згорани.doc", "zghorany"),
    ("Олексій звіт.docx", "oleksii zvit"),
])
def test_ukrainian_national_transliteration_finds_the_cyrillic_name(name, query):
    assert _finds(name, query)


def test_a_cyrillic_query_finds_a_name_in_ukrainian_transliteration():
    assert _finds("Kyiv 2023.jpg", "Київ")


def test_the_russian_style_keys_stay():
    keys = set(name_keys("Київ.jpg"))
    assert {"w:київ", "w:kiyiv", "w:kyiv"} <= keys


@pytest.mark.parametrize("name,query", [
    ("Report—final’s.docx", "report final"),
    ("Report–final.docx", "report final"),
    ("O’Brien.pdf", "o brien"),
    ("“Quoted” name.txt", "quoted name"),
    ("‘Single’ quotes.txt", "single quotes"),
    ("Пам’ять.md", "пам ять"),
    ("Памʼять.md", "пам ять"),
    ("„Deutsch“ notes.txt", "deutsch notes"),
    ("Sale 50%.pdf", "sale 50"),
])
def test_typographic_dashes_quotes_and_percent_separate_words(name, query):
    assert _finds(name, query)


def test_percent_is_not_part_of_the_number():
    assert "w:50" in name_keys("50%.pdf")


def test_variant_whole_keys_come_before_any_trigram_and_originals_first():
    q = query_name_keys("Résumé Київ")
    whole = [k for k in q if k.startswith("w:")]
    assert q[: len(whole)] == tuple(whole)
    assert whole[:2] == ["w:résumé", "w:київ"]
    assert {"w:resume", "w:kyiv"} <= set(whole)


def test_cap_cuts_variant_trigrams_before_original_ones():
    q = query_name_keys("Résumé Київ відпустка двадцять")
    assert len(q) <= NAME_QUERY_KEYS_MAX
    original_grams = {f"t:{g}" for tok in ("résumé", "київ", "відпустка", "двадцять")
                      for g in (tok[i:i + 3] for i in range(len(tok) - 2))}
    assert original_grams <= set(q)


def test_query_token_groups_hold_every_variant_of_a_token():
    (group,) = query_token_groups("Київ")
    assert {"w:київ", "w:kiyiv", "w:kyiv"} <= group
    (group,) = query_token_groups("Résumé")
    assert {"w:résumé", "w:resume"} <= group


def test_ukrainian_spelling_adds_no_trigrams_so_unrelated_names_stay_below_the_floor():
    # "Подогрев" shares one real trigram with "Догов"; a third script form of it would pass the floor.
    assert "t:doh" not in name_keys("Договор.docx")
    assert name_match(name_keys("Подогрев 001.txt"), query_name_keys("Догов")) is None
    assert name_match(name_keys("Договор аренды.docx"), query_name_keys("Догов")) is not None


def test_unaccented_spelling_keeps_trigrams_for_typos():
    assert name_match(name_keys("Déclaration fiscale.pdf"), query_name_keys("declaraton")) is not None


@pytest.mark.parametrize("name,query", [
    # Spanish: á é í ó ú ü ñ
    ("Mamá.jpg", "mama"), ("Café.txt", "cafe"), ("Martín.pdf", "martin"), ("Canción.mp3", "cancion"),
    ("Perú viaje.jpg", "peru viaje"), ("Pingüino.png", "pinguino"), ("Año nuevo.jpg", "ano nuevo"),
    ("Mañana.docx", "manana"),
    # French: à â é è ê ë î ï ô ù û ü ÿ ç œ æ
    ("Voilà.txt", "voila"), ("Château.jpg", "chateau"), ("Été.jpg", "ete"), ("Mère.pdf", "mere"),
    ("Fête.png", "fete"), ("Noël.jpg", "noel"), ("Île de Ré.jpg", "ile de re"), ("Maïs.txt", "mais"),
    ("Hôtel.pdf", "hotel"), ("Où.txt", "ou"), ("Août.jpg", "aout"), ("Capharnaüm.mkv", "capharnaum"),
    ("L'Haÿ-les-Roses.jpg", "hay les roses"), ("Garçon.docx", "garcon"), ("Cœur.pdf", "coeur"),
    ("Œuvre complète.pdf", "oeuvre complete"), ("Lætitia.jpg", "laetitia"),
    # other Latin letters NFKD does not decompose
    ("Øresund.jpg", "oresund"), ("Łódź.jpg", "lodz"), ("Straße.pdf", "strasse"),
])
def test_spanish_and_french_names_are_found_unaccented(name, query):
    assert _finds(name, query)


@pytest.mark.parametrize("name,query", [("Cœur.pdf", "cœur"), ("Año.pdf", "año"), ("Été.jpg", "été")])
def test_the_accented_original_key_stays(name, query):
    assert f"w:{query}" in name_keys(name)
    assert _finds(name, query)


def test_a_cyrillic_name_gets_no_stripped_keys():
    # й, ё and ї are letters, not accented и, е and і: only the token and its Latin transliterations.
    for name in ("Мой отчёт", "Київ", "Ёжик йод їжа"):
        cyrillic_whole = {k for k in name_keys(name) if k.startswith("w:") and not k[2:].isascii()}
        assert cyrillic_whole == {f"w:{t}" for t in name.casefold().split()}


@pytest.mark.parametrize("name,query", [
    ("¿Qué pasa?.txt", "que pasa"), ("¡Hola!.txt", "hola"), ("Notes…draft.md", "notes draft"),
    ("Col·lecció.pdf", "col leccio"), ("«Guerre» et paix.pdf", "guerre et paix"),
])
def test_spanish_and_typographic_punctuation_separate_words(name, query):
    assert _finds(name, query)


@pytest.mark.parametrize("name,query", [("Résumé.pdf", "resume"), ("Resume.pdf", "résumé")])
def test_the_unaccented_spelling_covers_the_query_both_ways(name, query):
    assert covers_query(name_keys(name), query_token_groups(query))
