"""Selectivity past the first term page (USER_DRIVE_INDEXING_RFC §24.6, arbiter R2 S-M1): a full first page that spans
few files says nothing about the files past it — the page is ordered by item_id, so a few long files with low ids can
fill it while the term sits in many more. The service probes past the page before deciding."""
from src.domain.drive_content_search import term_file_probes, term_selective
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import TERM_NONSELECTIVE_FILES


def _page(files, per_file):
    return [DriveChunk("u", f"i{f:03d}", 1, c, "", 0, 1, "pdf") for f in range(files) for c in range(per_file)]


def test_a_page_that_is_not_full_needs_no_probe():
    assert term_file_probes(_page(3, 2), limit=10) == 0
    assert term_file_probes([], limit=10) == 0


def test_a_full_page_over_many_files_is_decided_by_the_page():
    assert term_file_probes(_page(TERM_NONSELECTIVE_FILES + 1, 1), limit=TERM_NONSELECTIVE_FILES + 1) == 0


def test_a_full_page_from_few_files_probes_until_one_file_too_many():
    assert term_file_probes(_page(4, 25), limit=100) == TERM_NONSELECTIVE_FILES + 1 - 4
    assert term_file_probes(_page(1, 5), limit=5) == TERM_NONSELECTIVE_FILES   # never more than max_files probes
    assert term_file_probes(_page(2, 5), limit=10, max_files=3) == 2


def test_selective_counts_the_files_past_the_page():
    page = _page(4, 25)
    assert term_selective(page, 100, files_past_page=TERM_NONSELECTIVE_FILES - 4)        # exactly max_files: selects
    assert not term_selective(page, 100, files_past_page=TERM_NONSELECTIVE_FILES + 1 - 4)
    assert term_selective(_page(1, 5), 5, files_past_page=0)                              # an IBAN in one contract


def test_selective_without_probes_is_the_page_rule():
    many = _page(TERM_NONSELECTIVE_FILES + 1, 1)
    assert not term_selective(many, len(many), files_past_page=0)
    assert term_selective(many, len(many) + 1, files_past_page=0)   # not full: complete, selects
