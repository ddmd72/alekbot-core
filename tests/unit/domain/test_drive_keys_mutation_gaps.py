"""Boundaries of the Search v2 keyer that mutation testing showed unpinned (mutation_sv2.md, drive_keys): the year
range, the chunk key cap, the name-key classes, the hyphen compound's joined form, phrase runs, numbers' trigrams and
the name floor (USER_DRIVE_INDEXING_RFC §25.2.4, §25.2.6, §25.5.1, §25.5.4)."""
from datetime import datetime, timezone

import pytest

from src.domain import drive_keys
from src.domain.drive_index_params import NAME_MIN_TRIGRAMS
from src.domain.drive_key_sources import AboutField, RecordKeyInputs
from src.domain.drive_keys import (
    about_sources, name_keys_v2, name_match_v2, phrase_keys, query_concepts, query_name_keys_v2,
    source_class_of_name_key, word_keys, word_keys_capped,
)
from src.domain.drive_match import SourceClass


def _concept(query: str, word: str):
    return next(c for c in query_concepts(query).concepts if c.word == word)


# -- the year range 1900..2099, inclusive -----------------------------------------------------------------------------

@pytest.mark.parametrize("year,is_year", [(1899, False), (1900, True), (2099, True), (2100, False)])
def test_a_name_year_is_keyed_only_inside_the_range(year, is_year):
    assert (f"y:{year}" in name_keys_v2(f"Trip {year}.jpg")) is is_year


@pytest.mark.parametrize("year,is_year", [(1899, False), (1900, True), (2099, True), (2100, False)])
def test_a_month_year_is_keyed_only_inside_the_range(year, is_year):
    assert (f"m:{year}-03" in word_keys(f"Paid in March {year}.")) is is_year


@pytest.mark.parametrize("year,is_year", [(1899, False), (1900, True), (2099, True), (2100, False)])
def test_a_query_number_is_a_year_only_inside_the_range(year, is_year):
    assert (f"y:{year}" in _concept(f"receipts {year}", str(year)).about_keys) is is_year


# -- the chunk key cap: cut only past it -----------------------------------------------------------------------------

def test_the_chunk_cap_cuts_only_past_its_size(monkeypatch):
    text = "alpha bravo charlie delta echo"
    full, _ = word_keys_capped(text)
    monkeypatch.setattr(drive_keys, "WORD_KEYS_PER_CHUNK", len(full))
    assert word_keys_capped(text) == (full, False)
    monkeypatch.setattr(drive_keys, "WORD_KEYS_PER_CHUNK", len(full) - 1)
    assert word_keys_capped(text) == (full[:-1], True)


# -- name-key classes ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key,cls", [
    ("w:rome", SourceClass.VERIFIED_RECORD), ("n:12345", SourceClass.VERIFIED_RECORD),
    ("y:2024", SourceClass.VERIFIED_RECORD), ("x:rom", SourceClass.TRANSLATION), ("xl:rom", SourceClass.TRANSLATION),
    ("t:rom", None), ("m:2024-03", None), ("e:rome", None),
])
def test_a_name_keys_class_follows_from_its_prefix(key, cls):
    assert source_class_of_name_key(key) is cls


# -- phrases: the hyphen compound's joined form, runs and their last word --------------------------------------------

def test_a_hyphen_compound_spelling_also_keys_its_joined_word():
    keys = set(phrase_keys("Нью-Йорк"))
    assert {"e:нью_йорк", "e:ньюйорк"} <= keys


def test_a_compound_inside_a_longer_spelling_keys_no_joined_word():
    assert "e:ньюйорк" not in set(phrase_keys("Нью-Йорк Сити"))


@pytest.mark.parametrize("query,word,run", [
    ("notes from New York", "york", "e:new_york"),
    ("photos of the trip to Le Havre", "havre", "e:le_havr"),
])
def test_a_run_holding_a_later_word_is_keyed(query, word, run):
    assert run in _concept(query, word).about_keys


def test_a_run_ending_in_a_function_word_is_skipped():
    keys = _concept("Havre de Grace", "havre").about_keys
    assert "e:havr_de" not in keys and "e:havr_de_grac" in keys


@pytest.mark.parametrize("query,words", [
    ("Wi-Fi router", ["wifi", "router"]), ("router Wi-Fi", ["router", "wifi"]),
    ("router Wi-Fi password", ["router", "wifi", "password"]), ("e-mail", ["email"]),
])
def test_a_hyphen_compound_is_one_query_word_wherever_it_stands(query, words):
    assert [c.word for c in query_concepts(query).concepts] == words


def test_son_is_a_word_not_a_function_word():
    assert [c.word for c in query_concepts("son").concepts] == ["son"]


# -- the effective date's printed form -------------------------------------------------------------------------------

def test_the_effective_dates_source_prints_the_iso_day():
    rec = RecordKeyInputs(kind="pdf", effective_date=datetime(2026, 5, 1, 13, 30, tzinfo=timezone.utc))
    sources = about_sources(rec)
    (source,) = sources["y:2026"]
    assert source.field is AboutField.EFFECTIVE_DATE and source.printed == "2026-05-01"
    assert "m:2026-05" in sources


# -- the name stream's query keys and its floor ----------------------------------------------------------------------

def test_a_number_gives_the_name_stream_no_trigram():
    keys = query_name_keys_v2("report 2024")
    assert "t:rep" in keys and not [k for k in keys if k.startswith("t:") and k[2:].isdigit()]


def test_trigrams_alone_pass_the_floor_only_at_its_size():
    grams = [f"t:g{i:02d}" for i in range(NAME_MIN_TRIGRAMS)]
    assert name_match_v2(grams[:-1], grams[:-1]) is None
    assert name_match_v2(grams, grams) == NAME_MIN_TRIGRAMS


def test_one_whole_key_passes_the_floor_and_counts_every_shared_key():
    assert name_match_v2(["w:rome"], ["w:rome"]) == 1
    assert name_match_v2(["w:rome", "t:rom"], ["w:rome", "t:rom", "t:xyz"]) == 2
    assert name_match_v2(["w:rome"], ["w:paris"]) is None
