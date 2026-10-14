"""`selective_term_hits` (USER_DRIVE_INDEXING_RFC §24.6, review D-M2, arbiter R1-2): a term whose page came back full
AND spans more than TERM_NONSELECTIVE_FILES distinct files is left out."""
from src.domain.drive_content_search import selective_term_hits
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import TERM_NONSELECTIVE_FILES


def _chunks(n):
    return [DriveChunk("u", f"i{i:03d}", 1, 0, "", 0, 1, "pdf") for i in range(n)]


def test_a_full_page_is_not_selective_and_a_short_one_is():
    full = TERM_NONSELECTIVE_FILES + 1   # one chunk per file: a full page over more than 8 files
    hits = {"2024": _chunks(full), "98765": _chunks(1), "none": []}
    assert set(selective_term_hits(hits, full)) == {"98765", "none"}


def test_one_file_filling_the_page_is_still_a_full_page():
    # arbiter R1-2 reverses review D-M2: a full page from one file (an IBAN in every chunk) still selects
    hits = {"iban": [DriveChunk("u", "a", 1, i, "", 0, 1, "pdf") for i in range(5)]}
    assert selective_term_hits(hits, 5) == hits
