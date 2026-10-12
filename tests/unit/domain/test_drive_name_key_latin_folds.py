"""The unaccented name key drops every combining mark on a Latin letter, including one casefold leaves as its own
code point (İ → i + U+0307), and writes out Latin letters NFKD does not decompose (USER_DRIVE_INDEXING_RFC §3.2)."""
import pytest

from src.domain.drive_name_keys import covers_query, name_keys, name_match, query_name_keys, query_token_groups


def _finds(name: str, query: str) -> bool:
    record = name_keys(name)
    return name_match(record, query_name_keys(query)) is not None and covers_query(record, query_token_groups(query))


@pytest.mark.parametrize("name,query", [
    ("İzmir.jpg", "izmir"),            # casefold("İ") = "i" + U+0307
    ("İSTANBUL 2024.jpg", "istanbul"),
    ("Đorđević.pdf", "dordevic"),      # đ
    ("Guðrún.txt", "gudrun"),          # ð
    ("Þingvellir.jpg", "thingvellir"), # þ
    ("Iğdır.jpg", "igdir"),            # ı (and ğ through NFKD)
    ("Ħamrun.jpg", "hamrun"),          # ħ
])
def test_latin_letters_are_found_unaccented(name, query):
    assert _finds(name, query)


def test_a_combining_mark_after_a_non_latin_letter_is_kept():
    # U+0301 after a Cyrillic letter: not a Latin accent, so no stripped key is made.
    name = "ко\u0301т"
    cyrillic = {k for k in name_keys(name) if k.startswith("w:") and any("а" <= ch <= "я" for ch in k)}
    assert cyrillic == {f"w:{name}"}
