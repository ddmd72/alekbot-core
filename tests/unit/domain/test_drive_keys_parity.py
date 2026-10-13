"""Search v2 index/query parity (USER_DRIVE_INDEXING_RFC §25.2.4 P3: one normaliser): a word, a number or a named
thing the index stored is found by a query that types it, on randomised synthetic text (fixed seeds, no owner data).
Also `match_spans` (the proximity input of §25.5.4) and the chunk key cap (§25.2.6)."""
import random

import pytest

from src.domain.drive_index_params import WORD_KEYS_PER_CHUNK
from src.domain.drive_keys import (
    FUNCTION_WORDS, GENERIC_WORDS, match_spans, phrase_keys, query_concepts, token_spans, word_keys,
    word_keys_capped,
)

VOCABULARY = (
    "Барселона Барселону Барселоне Париж Париже Київ Києві Львів Харкові Иванов Ивановым Сергей Сергея музей музея "
    "отель отеля кровь крови Казань Казани квиток квитки чек чеки холестерин холестерину договор договора аренда "
    "аренды страховка страховки квартира квартиры паспорт паспорта лікарня лікарні рахунок рахунку подорож подорожі "
    "house houses lease leases bus buses glass glasses movie movies policy policies box boxes receipt receipts "
    "invoice invoices Paris analysis Barcelona Valencia contract contracts warranty insurance cholesterol hotel "
    "hoteles reserva reservas factura facturas entrada entradas viaje médico Résumé café naïve Zürich"
).split()
NUMBERS = ("1 250,00", "1,250.00", "INV-2024/0012", "15.03.2024", "2024-03-15", "15 March 2024", "12345",
           "+34 600 000 000", "2023", "7", "42")


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(VOCABULARY) for _ in range(n))


@pytest.mark.parametrize("seed", range(40))
def test_every_word_of_a_text_is_found_by_its_own_query(seed):
    rng = random.Random(seed)
    text = _sentence(rng, 25) + ", " + rng.choice(NUMBERS) + "; " + _sentence(rng, 10) + "."
    keys = set(word_keys(text))
    for token, start, end in token_spans(text):
        q = query_concepts(text[start:end])
        for concept in q.concepts:
            assert set(concept.word_keys) & keys, (text[start:end], concept)


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
    assert q.concepts
    for concept in q.concepts:
        assert set(concept.about_keys) & stored, (name, concept.word)


@pytest.mark.parametrize("seed", range(20))
def test_an_inflected_query_finds_a_text_holding_another_form(seed):
    """The pinned groups both ways, in random synthetic sentences."""
    groups = [("Барселона", "Барселоной"), ("Київ", "Києві"), ("Харків", "Харкові"), ("квиток", "квитки"),
              ("policy", "policies"), ("house", "houses"), ("cache", "caches"), ("Сергей", "Сергея")]
    rng = random.Random(2000 + seed)
    a, b = rng.choice(groups)
    if rng.random() < 0.5:
        a, b = b, a
    text = _sentence(rng, 8) + f" {b} " + _sentence(rng, 8)
    (concept,) = query_concepts(a).concepts
    assert set(concept.word_keys) & set(word_keys(text))


# -- match_spans ----------------------------------------------------------------------------------------------------

def test_match_spans_finds_each_occurrence_of_a_concepts_forms():
    text = "Hotel in Barcelona. The tickets: two tickets for Barcelona, total 1 250,00 EUR, March 2024."
    tickets = query_concepts("ticket").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, tickets.word_keys)] == ["tickets", "tickets"]
    barcelona = query_concepts("Барселона").concepts[0]                     # meets the Latin text by transliteration
    assert [text[s:e] for s, e in match_spans(text, barcelona.word_keys)] == ["Barcelona", "Barcelona"]
    amount = query_concepts("1250.00").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, amount.word_keys)] == ["1 250,00"]
    month = query_concepts("march 2024").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, month.word_keys)] == ["March 2024"]


def test_match_spans_of_a_key_the_text_lacks_is_empty():
    assert match_spans("nothing here", ("w:barcelona", "n:1250")) == ()


def test_match_spans_handles_decomposed_text():
    text = "Café Zürich"
    cafe = query_concepts("café").concepts[0]
    assert [text[s:e] for s, e in match_spans(text, cafe.word_keys)] == ["Café"]


# -- the chunk key cap ----------------------------------------------------------------------------------------------

def test_word_keys_are_capped_keeping_the_earliest_text():
    words = [f"w{chr(97 + i)}{chr(97 + j)}{chr(97 + k)}" for i in range(10) for j in range(10) for k in range(10)]
    text = " ".join(words)
    keys, capped = word_keys_capped(text)
    assert capped and len(keys) == WORD_KEYS_PER_CHUNK and word_keys(text) == keys
    assert "w:waaa" in keys and "w:wjjj" not in keys
    assert not word_keys_capped("a short text")[1]


def test_word_keys_hold_only_word_number_and_month_families():
    keys = word_keys("Invoice INV-7 of 15 March 2024, total 1 250,00; paid in April 2024.")
    assert {k.split(":", 1)[0] for k in keys} == {"w", "n", "m"}
    assert {"m:2024-03", "m:2024-04", "n:2024-03-15", "n:1250.00", "n:1250"} <= set(keys)


def test_word_keys_are_unique():
    keys = word_keys("receipt receipts Receipt RECEIPTS")
    assert len(keys) == len(set(keys))
