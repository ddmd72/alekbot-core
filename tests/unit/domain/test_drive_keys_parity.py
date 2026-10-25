"""Search v2 index/query parity (USER_DRIVE_INDEXING_RFC §25.2.4 P3: one normaliser): a word, a number or a named
thing the index stored is found by a query that types it, on randomised synthetic text (fixed seeds, no owner data).
Also `match_spans` (the proximity input of §25.5.4) and the chunk key cap (§25.2.6)."""
import random

import pytest

from src.domain.drive_index_params import WORD_KEYS_PER_CHUNK
from src.domain.drive_key_forms import SHORT_FUNCTION_WORDS
from src.domain.drive_keys import (
    FUNCTION_WORDS, GENERIC_WORDS, match_spans, phrase_keys, query_concepts, token_spans, word_keys,
    word_keys_capped,
)

VOCABULARY = (
    "Коїмбра Коїмбру Коїмбрі Берлін Берліні Одеса Одесі Львів Харкові Мельник Мельником Тарас Тараса музей музея "
    "отель отеля кровь крови Казань Казани квиток квитки чек чеки холестерин холестерину договор договора аренда "
    "аренды страховка страховки квартира квартиры паспорт паспорта лікарня лікарні рахунок рахунку подорож подорожі "
    "house houses lease leases bus buses glass glasses movie movies policy policies box boxes receipt receipts "
    "invoice invoices Berlin analysis Coimbra Porto contract contracts warranty insurance cholesterol hotel "
    "hoteles reserva reservas factura facturas entrada entradas viaje médico Résumé café naïve Zürich"
).split()
NUMBERS = ("1 250,00", "1,250.00", "INV-2024/0012", "15.03.2024", "2024-03-15", "15 March 2024", "12345",
           "+447 700 900 456", "2023", "7", "42")


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(VOCABULARY) for _ in range(n))


ADVERSARIAL = (
    "iPhone PayPal McDonalds OneDrive YouTube WiFi Wi-Fi e-mail E-Mail ИП ЖК ТВ CV TV EU HR X5 A4 ред3 "
    "комп'ютер комп’ютера сім'я сімʼї п'ять м'ясо ёлка елка счёт счет Лёд Лед за́мок квар­тира "
    "Нью-Йорк Нью-Йорке Санкт-Петербурге Odesa Одесі Kharkiv Харкові Lviv Львові São Zürich Œuvre straße"
).split()


def _recase(rng: random.Random, word: str) -> str:
    """The same word as a user might type it: lower, upper, capitalised, or as written."""
    return rng.choice([word, word.lower(), word.upper(), word[:1].upper() + word[1:].lower()])


def _removable(word: str) -> bool:
    return all(t in FUNCTION_WORDS or t in GENERIC_WORDS or len(t) == 1 or t in SHORT_FUNCTION_WORDS
               for t, _, _ in token_spans(word))


@pytest.mark.parametrize("seed", range(60))
def test_every_word_of_a_text_is_found_by_its_own_query(seed):
    """Index and query parity on adversarial input: per-occurrence casing, camelCase brands, apostrophes, hyphen
    compounds, 2-letter acronyms, ё/е, stress marks and soft hyphens (review M9). A query that yields no concept
    fails unless every token is a function or generic word."""
    rng = random.Random(seed)
    pool = VOCABULARY + ADVERSARIAL
    words = [_recase(rng, rng.choice(pool)) for _ in range(30)]
    text = " ".join(words[:20]) + ", " + rng.choice(NUMBERS) + "; " + " ".join(words[20:]) + "."
    keys = set(word_keys(text))
    for word in words:
        q = query_concepts(_recase(rng, word))
        assert q.concepts or _removable(word), word
        for concept in q.concepts:
            assert set(concept.word_keys) & keys, (word, concept.word)


@pytest.mark.parametrize("seed", range(10))
def test_parity_holds_on_a_text_past_the_cap_for_its_kept_keys(seed):
    """A text over the cap: the keys kept are the earliest text's, and every word of that early text is found."""
    rng = random.Random(500 + seed)
    def suffix(i: int) -> str:
        return chr(97 + i % 26) + chr(97 + i // 26 % 26) + chr(97 + i // 676)

    words = [rng.choice(VOCABULARY) + suffix(i) if i % 3 else rng.choice(VOCABULARY) for i in range(1600)]
    text = " ".join(words)
    keys, capped = word_keys_capped(text)
    assert capped
    for word in words[:50]:
        for concept in query_concepts(word).concepts:
            assert set(concept.word_keys) & set(keys), word


@pytest.mark.parametrize("number", NUMBERS)
def test_every_number_of_a_text_is_found_by_its_own_query(number):
    keys = set(word_keys(f"Total due {number} as agreed"))
    (concept,) = query_concepts(number).concepts
    assert set(concept.word_keys) & keys, (number, concept.word_keys)


@pytest.mark.parametrize("seed", range(40))
def test_a_named_thing_is_found_by_a_query_typing_it(seed):
    rng = random.Random(1000 + seed)
    words = [w for w in VOCABULARY if w.casefold() not in FUNCTION_WORDS | GENERIC_WORDS]
    name = " ".join(rng.choice(words) for _ in range(rng.randint(1, 3)))
    stored = set(phrase_keys(name))
    q = query_concepts(name)
    assert q.concepts, name
    for concept in q.concepts:
        assert set(concept.about_keys) & stored, (name, concept.word)


@pytest.mark.parametrize("seed", range(20))
def test_an_inflected_query_finds_a_text_holding_another_form(seed):
    """The pinned groups both ways, in random synthetic sentences."""
    groups = [("Коїмбра", "Коїмброю"), ("Київ", "Києві"), ("Харків", "Харкові"), ("квиток", "квитки"),
              ("policy", "policies"), ("house", "houses"), ("cache", "caches"), ("трофей", "трофея")]
    rng = random.Random(2000 + seed)
    a, b = rng.choice(groups)
    if rng.random() < 0.5:
        a, b = b, a
    text = _sentence(rng, 8) + f" {b} " + _sentence(rng, 8)
    (concept,) = query_concepts(a).concepts
    assert set(concept.word_keys) & set(word_keys(text))


# -- match_spans ----------------------------------------------------------------------------------------------------

def test_match_spans_finds_each_occurrence_of_a_concepts_forms():
    text = "Hotel in Coimbra. The tickets: two tickets for Coimbra, total 1 250,00 EUR, March 2024."
    tickets = query_concepts("ticket").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, tickets.word_keys)] == ["tickets", "tickets"]
    coimbra = query_concepts("Коїмбра").concepts[0]                       # meets the Latin text by transliteration
    assert [text[s:e] for s, e in match_spans(text, coimbra.word_keys)] == ["Coimbra", "Coimbra"]
    amount = query_concepts("1250.00").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, amount.word_keys)] == ["1 250,00"]
    month = query_concepts("march 2024").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, month.word_keys)] == ["March 2024"]


def test_match_spans_of_a_key_the_text_lacks_is_empty():
    assert match_spans("nothing here", ("w:coimbra", "n:1250")) == ()


def test_match_spans_handles_decomposed_text():
    text = "Café Zürich"
    cafe = query_concepts("café").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, cafe.word_keys)] == ["Café"]


# -- the chunk key cap ----------------------------------------------------------------------------------------------

def test_word_keys_are_capped_keeping_the_earliest_text():
    words = [f"w{chr(97 + i)}{chr(97 + j)}{chr(97 + k)}" for i in range(26) for j in range(26) for k in range(4)]
    text = " ".join(words)
    keys, capped = word_keys_capped(text)
    assert capped and len(keys) == WORD_KEYS_PER_CHUNK and word_keys(text) == keys
    assert "w:waaa" in keys and "w:wzzd" not in keys
    assert not word_keys_capped("a short text")[1]


def test_word_keys_hold_only_word_number_and_month_families():
    keys = word_keys("Invoice INV-7 of 15 March 2024, total 1 250,00; paid in April 2024.")
    assert {k.split(":", 1)[0] for k in keys} == {"w", "xl", "n", "m"}   # xc F3: a Latin text's skeletons are `xl:`
    assert {"m:2024-03", "m:2024-04", "n:2024-03-15", "n:1250.00", "n:1250"} <= set(keys)


def test_word_keys_are_unique():
    keys = word_keys("receipt receipts Receipt RECEIPTS")
    assert len(keys) == len(set(keys))
