"""Term selectivity counts DISTINCT FILES on the full first page, not chunks (arbiter R1-2, fixing review D-M2;
USER_DRIVE_INDEXING_RFC §24.6).

A full term page is an id-ordered slice, so a term spread over many files ranks them by id: such a term is dropped
from the fusion, the window anchoring, the trimming and the widening. But a term concentrated in one file (an IBAN
repeated in 120 chunks of one contract) fills the page too, and that page is the file the user asked for: it still
fuses and anchors. A term is non-selective only when its full first page spans more than
TERM_NONSELECTIVE_FILES distinct files; the folder in-memory path applies the same rule."""
from unittest.mock import MagicMock

from src.domain.drive_content_search import selective_term_hits
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_params import CONTENT_TERM_FETCH, FETCH_MAX, TERM_NONSELECTIVE_FILES
from src.services import drive_search_service as search_mod

from .test_drive_search_content_streams import FAR, by_id, index, query_vector, settle_metadata, world

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

async def test_an_iban_in_120_chunks_of_one_contract_is_found_and_anchors_its_window(monkeypatch):
    w = world()
    contract = w.drive.add("contract.pdf")
    others = [w.drive.add(f"note-{i}.pdf") for i in range(3)]
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    lines = [f"clause {i} pay to account {IBAN} monthly" for i in range(_CHUNKS)]
    await index(w, contract, lines, [FAR] * len(lines))
    for doc in others:
        await index(w, doc, ["minutes of the meeting"], [FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search(IBAN)
    assert [c.item_id for c in result.cards] == [contract]
    assert "clause 0 pay to account" in by_id(result)[contract].window   # the term chunk anchors
    assert "term_kept=1" in log.info.call_args[0][0]


async def test_a_concentrated_term_anchors_over_the_nearest_chunk(monkeypatch):
    w = world()
    statement = w.drive.add("statement.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "overdraft fee")
    lines = [f"line {i} account {IBAN} transfer" for i in range(_CHUNKS)]
    await index(w, statement, lines + ["overdraft fee charged this month"], [FAR] * len(lines) + [q])
    result = await w.search(f"overdraft fee {IBAN}")
    assert "line 0 account" in by_id(result)[statement].window


async def test_a_year_on_30_files_is_dropped(monkeypatch):
    w = world()
    decoys = [w.drive.add(f"a-report-{i:02d}.pdf") for i in range(30)]   # their ids sort before the target's
    target = w.drive.add("z-contract.pdf")
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in decoys:                          # 30 files x 4 chunks: the first page of "2024" is full
        await index(w, doc, [f"annual report 2024 part {p}" for p in range(4)], [FAR] * 4)
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search("2024 98765")
    assert [c.item_id for c in result.cards] == [target]
    assert "term_kept=1" in log.info.call_args[0][0]
    assert ("2024", FETCH_MAX) not in [(t, limit) for t, limit, _ in w.repo.term_calls]   # not widened


# --- the folder in-memory path ---------------------------------------------------------------------------------

async def test_folder_in_memory_an_iban_in_one_contract_is_found_and_anchors(monkeypatch):
    w = world()
    work = w.drive.add("Work", folder=True)
    contract = w.drive.add("contract.pdf", parent=work)
    others = [w.drive.add(f"note-{i}.pdf", parent=work) for i in range(3)]
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    lines = [f"clause {i} pay to account {IBAN} monthly" for i in range(_CHUNKS)]
    await index(w, contract, lines, [FAR] * len(lines))
    for doc in others:
        await index(w, doc, ["minutes of the meeting"], [FAR])
    result = await w.search(IBAN, folder="Work")
    assert [c.item_id for c in result.cards] == [contract]
    assert "clause 0 pay to account" in by_id(result)[contract].window


async def test_folder_in_memory_a_year_on_30_files_is_dropped(monkeypatch):
    w = world()
    work = w.drive.add("Work", folder=True)
    decoys = [w.drive.add(f"a-report-{i:02d}.pdf", parent=work) for i in range(30)]
    target = w.drive.add("z-contract.pdf", parent=work)
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in decoys:
        await index(w, doc, [f"annual report 2024 part {p}" for p in range(4)], [FAR] * 4)
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    result = await w.search("2024 98765", folder="Work")
    assert [c.item_id for c in result.cards] == [target]
