"""Name keys v2 (USER_DRIVE_INDEXING_RFC §25.2.4 "Name keys", NAME_KEYS_VERSION 2): the native `w:` forms of every
token, one `x:` skeleton per word (v1's transliterated `w:` variants are dropped, P1 review B2/M8), the `n:`/`y:` forms
of digit tokens and number terms, and trigrams, so a concept's keys meet a name the way they meet a passage. v1
v1 `name_keys` is retired (no caller in src/)."""
import re

import pytest

from src.domain.drive_keys import NAME_KEYS_V2, name_keys_v2, query_concepts, token_forms, token_spans


@pytest.mark.parametrize("name", ["Барселоне 2023.jpg", "Receipts March.pdf", "IMG_2210.jpg", "Київ — план v2.docx",
                                  "Crème brûlée.md", "myFileName Нью-Йорк сім'я.txt", ""])
def test_v2_keys_every_token_but_no_transliterated_spelling(name):
    """v2 replaces v1's transliterated `w:` variants by one `x:` skeleton per word (review B2/M8): a name matched
    only through another script is a translation, not exact. Kept, per §25.2.4 "Name keys": every native `w:` form
    of every token (with case parts and compounds), the `x:` skeleton of each word, and the trigrams of each token;
    the `w:` keys are exactly the native forms (test reviewer: restores the full-set check the v1-subset test made)."""
    from src.domain.drive_key_forms import scan, skeleton_key
    from src.domain.drive_name_keys import _strip_accents, _trigrams
    keys = set(name_keys_v2(name))
    for token, _, _ in token_spans(name):
        forms = token_forms(token)
        assert (f"w:{forms[0]}" in keys) if forms else (f"n:{token}" in keys or len(token) < 2), token
    native, skeletons, grams = set(), set(), set()
    for token in scan(name):
        for word in (token.text, *token.parts, token.compound):
            if word:
                native.update(token_forms(word))
        for word in (token.text, token.compound):
            sk = skeleton_key(word) if word else None
            if sk:    # xc F3: a skeleton is keyed under the script of its word, `x:` Cyrillic and `xl:` Latin
                skeletons.add(("x:" if re.search("[\u0400-\u04ff]", word) else "xl:") + sk)
        grams.update(_trigrams(_strip_accents(token.text)))
    assert {k[2:] for k in keys if k.startswith("w:")} == native
    assert skeletons <= keys and {"t:" + g for g in grams} <= keys
    assert bool(skeletons) == bool(name)


def test_v2_has_no_cross_script_spelling_under_w():
    assert not {"w:kyiv", "w:kiyiv"} & set(name_keys_v2("Київ.docx"))
    assert "w:barselone" not in name_keys_v2("Барселоне.jpg")


def test_v2_is_sorted_and_unique():
    keys = name_keys_v2("Receipts receipts 2023.pdf")
    assert list(keys) == sorted(set(keys))


def test_v2_adds_word_forms():
    keys = set(name_keys_v2("Барселоне 2023.jpg"))
    assert {"w:барселон", "x:barselon"} <= keys


def test_v2_adds_number_and_year_forms_of_digit_tokens():
    keys = set(name_keys_v2("Trip 2023 v2 IMG_2210.jpg"))
    assert {"n:2023", "y:2023", "w:2", "n:2210", "n:img2210"} <= keys
    assert "y:2210" not in keys


@pytest.mark.parametrize("name,query", [
    ("Receipt 2023-05.pdf", "receipts"),
    ("Барселона.jpg", "Барселоне"),
    ("Barcelona trip.docx", "Барселона"),
    ("Policies.pdf", "policy"),
    ("Trip 2023.md", "2023"),
    ("IMG_2210.jpg", "IMG_2210"),
    ("IMG_2210.jpg", "2210"),
])
def test_a_concept_meets_a_v2_name(name, query):
    keys = set(name_keys_v2(name))
    for c in query_concepts(query).concepts:
        assert (set(c.word_keys) | set(c.about_keys)) & keys, (name, query, c)


def test_the_target_version_is_two():
    assert NAME_KEYS_V2 == 2
