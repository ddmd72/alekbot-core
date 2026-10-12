"""Function words in a query ("the", "from", "my", "из", "з") are not name words: they neither block the
"covers every query token" tie-break nor take a whole-token query slot (USER_DRIVE_INDEXING_RFC §10.1 step 3a)."""
import pytest

from src.domain.drive_name_keys import covers_query, name_keys, query_name_keys, query_token_groups


@pytest.mark.parametrize("query,name", [
    ("the lease agreement", "Lease agreement.pdf"),
    ("photos from Rome", "Rome photos.zip"),
    ("my budget for 2026", "budget 2026.ods"),
    ("мой договор аренды", "Договор аренды.docx"),
    ("фото из Порту", "Порту фото.jpg"),
    ("ідеї для проєкту", "Ідеї проєкту.md"),
    ("план з бюджету", "План бюджету.xlsx"),
])
def test_a_name_with_every_content_word_covers_the_query(query, name):
    assert covers_query(name_keys(name), query_token_groups(query))


@pytest.mark.parametrize("query,stop", [
    ("the lease of my flat", {"w:the", "w:of", "w:my"}),
    ("фото из Рима", {"w:из"}),
    ("файли з архіву", {"w:з"}),
])
def test_function_words_take_no_query_key(query, stop):
    keys = set(query_name_keys(query))
    assert not keys & stop
    assert any(k.startswith("w:") for k in keys)


def test_a_query_of_function_words_only_keeps_them():
    """Nothing else to search for: "The The" is a band, "My" may be a folder."""
    assert "w:my" in query_name_keys("my")
    assert covers_query(name_keys("The The - live.mp3"), query_token_groups("the the"))


def test_record_keys_are_unchanged():
    assert "w:the" in name_keys("The lease.pdf") and "w:из" in name_keys("фото из Рима.jpg")
