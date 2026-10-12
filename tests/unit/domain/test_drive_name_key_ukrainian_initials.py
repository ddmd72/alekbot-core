"""The Ukrainian national transliteration's word-initial forms (я→ya, ю→yu, й→y, є→ye at the start, ia/iu/i/ie
inside), on names the Russian-style key cannot match (USER_DRIVE_INDEXING_RFC §3.2)."""
import pytest

from src.domain.drive_name_keys import covers_query, name_keys, name_match, query_name_keys, query_token_groups


@pytest.mark.parametrize("name,query,russian_style", [
    ("Яготин.jpg", "yahotyn", "yagotin"),
    ("Юрій.jpg", "yurii", "yuriy"),
    ("Йосипівка.jpg", "yosypivka", "yosipivka"),
    ("Євпаторія.jpg", "yevpatoriia", "yevpatoriya"),
])
def test_word_initial_forms_match_as_a_whole_key(name, query, russian_style):
    record = name_keys(name)
    assert f"w:{query}" in record and f"w:{russian_style}" in record and query != russian_style
    assert name_match(record, query_name_keys(query)) is not None
    assert covers_query(record, query_token_groups(query))
