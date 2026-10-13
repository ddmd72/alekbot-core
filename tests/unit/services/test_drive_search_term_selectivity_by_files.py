"""Term selectivity counts DISTINCT FILES on the full first page, not chunks (arbiter R1-2, fixing review D-M2;
USER_DRIVE_INDEXING_RFC §24.6).

A full term page is an id-ordered slice, so a term spread over many files ranks them by id: such a term is dropped
from the fusion, the window anchoring, the trimming and the widening. But a term concentrated in one file (an IBAN
repeated in 120 chunks of one contract) fills the page too, and that page is the file the user asked for: it still
fuses and anchors. A term is non-selective only when its full first page spans more than
TERM_NONSELECTIVE_FILES distinct files; the folder in-memory path applies the same rule.

Search v2 (§25.9) retired the rule in the search: its service tests were replaced by commonness by files in
test_drive_search_v2_ported.py. The domain-rule tests below stay until `drive_content_search` is removed."""

from src.domain.drive_content_search import selective_term_hits
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import CONTENT_TERM_FETCH, TERM_NONSELECTIVE_FILES


# A synthetic IBAN-shaped string (no real account), assembled so no source line holds it whole.
IBAN = " ".join(("ZZ07", "0000", "1111", "2222", "3333", "44"))
_CHUNKS = CONTENT_TERM_FETCH + 20          # more chunks than one term page holds


def _vectors_off(w):
    """Refreshing (the index's vectors in another space): only the name and term streams run."""
    state = w.repo.states["u"]
    w.repo.states["u"] = type(state)(**{**state.__dict__, "applied_space": "another-space/8"})


def _spread(files, per_file):
    return [DriveChunk("u", f"i{f:03d}", 1, c, "", 0, 1, "pdf") for f in range(files) for c in range(per_file)]


# --- the domain rule ---------------------------------------------------------------------------------------------

def test_the_threshold_is_eight_files():
    assert TERM_NONSELECTIVE_FILES == 8


def test_a_full_page_from_one_file_is_selective():
    hits = {"iban": [DriveChunk("u", "contract", 1, i, "", 0, 1, "pdf") for i in range(CONTENT_TERM_FETCH)]}
    assert set(selective_term_hits(hits, CONTENT_TERM_FETCH)) == {"iban"}


def test_a_full_page_over_more_than_eight_files_is_not_selective_and_eight_files_is():
    nine = _spread(9, 4)[:33]                  # a full page of 33 chunks over nine files
    eight = _spread(8, 4) + _spread(1, 1)      # a full page of 33 chunks over eight files
    assert len({c.item_id for c in nine}) == 9 and len({c.item_id for c in eight}) == 8
    assert set(selective_term_hits({"nine": nine, "eight": eight}, 33)) == {"eight"}


def test_a_page_that_is_not_full_is_selective_however_many_files():
    assert set(selective_term_hits({"2024": _spread(30, 1)}, 100)) == {"2024"}


# --- the user-wide streams -------------------------------------------------------------------------------------


# --- the folder in-memory path ---------------------------------------------------------------------------------


