"""A query term whose first page came back full is not selective (USER_DRIVE_INDEXING_RFC §24.6, review D-M2).

The term page is ordered by item_id, so a full page holds an arbitrary slice of the files sharing the term: ranking
them would be ranking by id, and anchoring their windows on the term's chunk would show the user a line that holds
"2024" instead of the passage the query is about. Such a term is left out of the fusion and of window anchoring, and
it is not read again on the widening; a term whose page was not full is complete and keeps both."""
from unittest.mock import MagicMock

from src.domain.drive_index_params import CONTENT_TERM_FETCH
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
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", 4)
    w = world()
    decoys = [w.drive.add(f"a-report-{i}.pdf") for i in range(6)]   # their ids sort before the target's
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
    assert [(t, limit) for t, limit, _ in w.repo.term_calls] == [("98765", 4), ("2024", 4)]   # no re-read
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
    assert "overdraft fee charged this month" in hit.window   # the kNN chunk anchors, not the first IBAN line
    assert "line 0 " not in hit.window
    assert "term_kept=0" in log.info.call_args[0][0]
    assert len(w.repo.term_calls) == 1                        # the full page is not read again on the widening


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
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", 4)
    w = world()
    work = w.drive.add("Work", folder=True)
    decoys = [w.drive.add(f"a-report-{i}.pdf", parent=work) for i in range(6)]
    target = w.drive.add("z-contract.pdf", parent=work, facets=taken("2026-03-05"))
    await settle_metadata(w, monkeypatch)
    _vectors_off(w)
    for doc in decoys:
        await index(w, doc, ["annual report 2024"], [FAR])
    await index(w, target, ["contract 98765 signed in 2024"], [FAR])
    result = await w.search("2024 98765", folder="Work")
    assert [c.item_id for c in result.cards] == [target]
