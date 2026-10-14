"""A query term whose full first page spans many files is not selective (USER_DRIVE_INDEXING_RFC §24.6, review D-M2;
arbiter R1-2: more than TERM_NONSELECTIVE_FILES distinct files, not chunks).

The term page is ordered by item_id, so a full page spread over many files holds an arbitrary slice of the files
sharing the term: ranking them would be ranking by id, and anchoring their windows on the term's chunk would show the
user a line that holds "2024" instead of the passage the query is about. Such a term is left out of the fusion and of
window anchoring, and it is not read again on the widening; a term whose page was not full is complete and keeps
both, and so does a term whose full page comes from few files (an IBAN in every chunk of one statement)."""
from unittest.mock import MagicMock

from src.domain.drive_index_params import CONTENT_TERM_FETCH, TERM_NONSELECTIVE_FILES
from src.services import drive_search_service as search_mod

from .test_drive_search_content_streams import FAR, by_id, index, query_vector, settle_metadata, world
from .test_drive_search_service import taken

# A synthetic IBAN-shaped string (no real account), assembled so no source line holds it whole.
IBAN = " ".join(("ZZ07", "0000", "1111", "2222", "3333", "44"))


def _vectors_off(w):
    """Refreshing (the index's vectors in another space): only the name and term streams run."""
    state = w.repo.states["u"]
    w.repo.states["u"] = type(state)(**{**state.__dict__, "applied_space": "another-space/8"})


async def test_a_year_on_many_documents_does_not_rank_them(monkeypatch):
    fetch = TERM_NONSELECTIVE_FILES + 2   # a full first page of one chunk per file spans more than 8 files (R1-2)
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", fetch)
    w = world()
    decoys = [w.drive.add(f"a-report-{i:02d}.pdf") for i in range(fetch + 2)]   # their ids sort before the target's
    target = w.drive.add("z-contract.pdf")
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in decoys:
        await index(w, doc, ["annual report 2024"], [FAR])
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search("2024 98765")
    assert [c.item_id for c in result.cards] == [target]
    assert [(t, limit) for t, limit, _ in w.repo.term_calls] == [("98765", fetch), ("2024", fetch)]   # no re-read
    assert "term_kept=1" in log.info.call_args[0][0]


async def test_a_term_whose_page_is_not_full_still_ranks_every_file_holding_it(monkeypatch):
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", 4)
    w = world()
    docs = [w.drive.add(f"report-{i}.pdf") for i in range(3)]
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in docs:
        await index(w, doc, ["annual report 2024"], [FAR])
    result = await w.search("2024")
    assert sorted(c.item_id for c in result.cards) == sorted(docs)


async def test_an_iban_repeated_in_every_chunk_of_a_statement_does_not_anchor_its_window(monkeypatch):
    # arbiter R1-2 reverses review D-M2 here: the IBAN's full page comes from ONE file, so the term selects — it is
    # kept, it anchors the statement's window on its chunk, and its full page is read again, wider, on the widening
    w = world()
    statement = w.drive.add("statement.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "overdraft fee")
    lines = [f"line {i} account {IBAN} transfer" for i in range(CONTENT_TERM_FETCH + 20)]
    await index(w, statement, lines + ["overdraft fee charged this month"], [FAR] * len(lines) + [q])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search(f"overdraft fee {IBAN}")
    hit = by_id(result)[statement]
    assert "line 0 account" in hit.window                     # the term chunk anchors, not the kNN chunk
    assert "term_kept=1" in log.info.call_args[0][0]
    limits = [limit for _, limit, _ in w.repo.term_calls]
    assert len(limits) == 2 and limits[0] == CONTENT_TERM_FETCH < limits[1]   # the full selective page, widened


async def test_a_selective_term_anchors_the_window_on_its_chunk(monkeypatch):
    w = world()
    doc = w.drive.add("contract.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "penalty clause")
    texts = [f"section {i}" for i in range(6)] + ["penalty clause applies"]
    texts[2] = "invoice 98765 attached"
    await index(w, doc, texts, [FAR] * 6 + [q])
    result = await w.search("penalty clause 98765")
    assert "invoice 98765 attached" in by_id(result)[doc].window


async def test_the_folder_in_memory_ranking_drops_a_term_on_too_many_chunks(monkeypatch):
    fetch = TERM_NONSELECTIVE_FILES + 2   # the same rule as the user-wide stream: a full page over more than 8 files
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", fetch)
    w = world()
    work = w.drive.add("Work", folder=True)
    decoys = [w.drive.add(f"a-report-{i:02d}.pdf", parent=work) for i in range(fetch + 2)]
    target = w.drive.add("z-contract.pdf", parent=work, facets=taken("2026-03-05"))
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in decoys:
        await index(w, doc, ["annual report 2024"], [FAR])
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    result = await w.search("2024 98765", folder="Work")
    assert [c.item_id for c in result.cards] == [target]
