"""Word-initial ї is yi in the Ukrainian national transliteration (USER_DRIVE_INDEXING_RFC §3.2)."""
from src.domain.drive_name_keys import covers_query, name_keys, name_match, query_name_keys, query_token_groups


def test_initial_yi_matches_as_a_whole_key():
    record = name_keys("Їжакевич.jpg")
    assert "w:yizhakevych" in record and "w:yizhakevich" in record   # Ukrainian, and the Russian-style form
    assert name_match(record, query_name_keys("yizhakevych")) is not None
    assert covers_query(record, query_token_groups("yizhakevych"))
