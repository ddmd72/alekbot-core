"""`selective_term_hits` (USER_DRIVE_INDEXING_RFC §24.6, review D-M2): a term whose page came back full is left out."""
from src.domain.drive_content_search import selective_term_hits
from src.domain.drive_index import DriveChunk


def _chunks(n):
    return [DriveChunk("u", f"i{i:03d}", 1, 0, "", 0, 1, "pdf") for i in range(n)]


def test_a_full_page_is_not_selective_and_a_short_one_is():
    hits = {"2024": _chunks(4), "98765": _chunks(1), "none": []}
    assert set(selective_term_hits(hits, 4)) == {"98765", "none"}


def test_one_file_filling_the_page_is_still_a_full_page():
    hits = {"iban": [DriveChunk("u", "a", 1, i, "", 0, 1, "pdf") for i in range(5)]}
    assert selective_term_hits(hits, 5) == {}
