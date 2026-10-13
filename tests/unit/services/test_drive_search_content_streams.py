"""DriveSearchService with the content streams (USER_DRIVE_INDEXING_RFC §7.4, §10.1 step 3, §10.2, §17.4, §17.6,
§24.6). The records come from the real tick (metadata only); chunks are written straight into the repository and the
records marked readable, as the index run (plan part B) will. The policy admits content by monkeypatch."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_content_terms import content_terms
from src.domain.drive_index import DriveChunk, RecordState
from src.domain.drive_index_params import CHUNK_FETCH, CONTENT_TERM_QUERIES, FETCH_MAX
from src.domain.drive_search import MATCHED_BOTH, MATCHED_CONTENT, render_results
from src.services import drive_search_service as search_mod

from .drive_index_fakes import ROOT
from .test_drive_search_service import SpyRepo, World, taken

MARCH = dict(date_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
             date_to=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))
FAR = [-1.0] * 8


class ContentSpy(SpyRepo):
    """Records the content-stream calls; stream reads come back without text, as Firestore's projection does."""

    def __init__(self) -> None:
        super().__init__()
        self.chunk_limits = []
        self.term_calls = []
        self.window_calls = []

    async def find_nearest_chunks(self, user_id, vector, limit, kind=None):
        self.chunk_limits.append((limit, kind))
        hits = await super().find_nearest_chunks(user_id, vector, limit, kind)
        return [(_blank(c), d) for c, d in hits]

    async def find_chunks_by_term(self, user_id, term, limit, kind=None):
        self.term_calls.append((term, limit, kind))
        return [_blank(c) for c in await super().find_chunks_by_term(user_id, term, limit, kind)]

    async def get_chunks(self, user_id, item_id, generation, indexes):
        self.window_calls.append((item_id, generation, tuple(indexes)))
        return await super().get_chunks(user_id, item_id, generation, indexes)


def _blank(c):
    return DriveChunk(c.user_id, c.item_id, c.generation, c.index, "", c.char_start, c.char_end, c.kind,
                      c.content_terms)


@pytest.fixture(autouse=True)
def admit(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)


def world(**kw):
    w = World(**kw)
    spy = ContentSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


async def settle_metadata(w, monkeypatch):
    """The Slice 1 tick builds the records; the content kinds are admitted only for the search."""
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    await w.settle()
    monkeypatch.setattr(policy, "ADMITTED_KINDS", policy.SLICE2_CONTENT_KINDS)


async def query_vector(w, query):
    return await w.emb.get_embedding(query, task_type="RETRIEVAL_QUERY")


async def index(w, item_id, texts, vectors, gen=1, readable=True):
    """Write one generation of chunks (with real offsets and content terms); mark it readable unless told not to."""
    rec = w.repo.records[("u", item_id)]
    chunks, start = [], 0
    for i, text in enumerate(texts):
        chunks.append(DriveChunk("u", item_id, gen, i, text, start, start + len(text), rec.kind, content_terms(text)))
        start += len(text)
    await w.repo.write_chunks(chunks, vectors)
    if readable:
        w.set_state(item_id, state=RecordState.INDEXED, index_generation=gen, indexed_generation=gen,
                    indexed_version=rec.content_version, chunk_count=len(texts))


def by_id(result):
    return {c.item_id: c for c in result.cards}


# --- the chunk stream, the window and `matched` -----------------------------------------------------------------

async def test_a_weakly_named_scan_is_found_by_its_content_with_a_window(monkeypatch):
    w = world()
    scan = w.drive.add("Scan_0012.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "notice period for termination")
    await index(w, scan, ["1. Parties. ", "7.2 Either party may terminate on 30 days notice. ", "8. Law."],
                [FAR, q, FAR])
    result = await w.search("notice period for termination")
    hit = by_id(result)[scan]
    assert hit.matched in (MATCHED_CONTENT, MATCHED_BOTH)
    assert hit.window == "1. Parties. 7.2 Either party may terminate on 30 days notice. 8. Law."
    assert w.repo.window_calls == [(scan, 1, (0, 1, 2))]   # the stream's chunk had no text: read by id
    assert result.envelope.content_search
    assert "matching passage:\n1. Parties. 7.2 Either party" in render_results(result)


async def test_content_streams_do_not_run_while_nothing_is_admitted(monkeypatch):
    w = world()
    scan = w.drive.add("Scan_0012.pdf")
    await settle_metadata(w, monkeypatch)
    await index(w, scan, ["Invoice INV-2024/0012"], [await query_vector(w, "INV-2024/0012")])
    monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset())
    monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset())   # since the flip images are triaged (§24.1)
    result = await w.search("INV-2024/0012")
    assert w.repo.chunk_limits == [] and w.repo.term_calls == [] and w.repo.window_calls == []
    assert not result.envelope.content_search and all(c.window == "" for c in result.cards)


# --- the read fence (§7.4, §17.4) ----------------------------------------------------------------------------

async def test_reindex_at_the_same_ctag_to_fewer_chunks_never_serves_the_old_generation(monkeypatch):
    w = world()
    doc = w.drive.add("contract.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "penalty")
    await index(w, doc, ["old one", "old two", "OLD PENALTY CLAUSE"], [FAR, FAR, q], gen=1)
    await index(w, doc, ["new single chunk"], [[0.5] * 8], gen=2)   # same cTag, fewer chunks
    result = await w.search("penalty")
    hit = by_id(result)[doc]
    assert hit.window == "new single chunk"
    assert "OLD" not in render_results(result) and "old one" not in render_results(result)
    assert w.repo.window_calls == [(doc, 2, (0, 1))]   # the neighbour is asked for at generation 2 only


async def test_a_crash_after_n_chunk_writes_serves_only_the_previous_generation(monkeypatch):
    w = world()
    doc = w.drive.add("contract.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "penalty")
    await index(w, doc, ["v1 text a", "v1 text b"], [[0.5] * 8, [0.5] * 8], gen=1)
    await index(w, doc, ["v2 partial PENALTY", "v2 partial two"], [q, q], gen=2, readable=False)   # crashed run
    w.set_state(doc, index_generation=2)   # persisted before the first chunk write (§8.1 step 2)
    result = await w.search("penalty")
    assert by_id(result)[doc].window == "v1 text av1 text b"
    assert "v2 partial" not in render_results(result)


async def test_a_demoted_or_deleting_record_shows_no_content(monkeypatch):
    w = world()
    demoted, deleting = w.drive.add("a.pdf"), w.drive.add("b.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "secret clause")
    await index(w, demoted, ["DEMOTED TEXT"], [q])
    await index(w, deleting, ["DELETING TEXT"], [q])
    w.set_state(demoted, state=RecordState.METADATA_ONLY, indexed_generation=None)
    w.set_state(deleting, state=RecordState.DELETING)
    result = await w.search("secret clause")
    text = render_results(result)
    assert "DEMOTED TEXT" not in text and "DELETING TEXT" not in text
    assert all(c.matched != MATCHED_CONTENT for c in result.cards)


# --- pooling before RRF (§17.6) and the starving document ----------------------------------------------------

async def test_many_hits_of_one_document_count_once_in_the_fusion(monkeypatch):
    w = world()
    many, both = w.drive.add("notes.pdf"), w.drive.add("budget memo.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "quarterly budget")
    await index(w, many, [f"part {i}" for i in range(5)], [q] * 5)       # chunk ranks 1-5
    await index(w, both, ["budget memo"], [[x + 0.3 * (i % 2) for i, x in enumerate(q)]])   # chunk rank 6
    w.repo.vectors[("u", both)] = list(q)    # card rank 1, and a name match
    w.repo.vectors[("u", many)] = FAR        # card rank 2
    result = await w.search("quarterly budget")
    # pooled: both = 1/61 (card) + 1/61 (name) + 1/62 (chunk) > many = 1/62 + 1/61;
    # unpooled, many's five chunks (1/61 + ... + 1/65 + 1/62) would outvote both
    assert [c.item_id for c in result.cards][:2] == [both, many]
    assert by_id(result)[both].matched == MATCHED_BOTH


async def test_a_400_chunk_document_does_not_starve_others_under_a_filter(monkeypatch):
    w = world()
    big = w.drive.add("handbook.pdf")                              # dated October: the filter drops it
    small = w.drive.add("march-note.pdf", facets=taken("2026-03-05"))
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "vacation policy")
    await index(w, big, [f"handbook section {i}" for i in range(400)], [q] * 400)
    await index(w, small, ["vacation policy for March"], [[x * 0.5 + 0.1 for x in q]])
    result = await w.search("vacation policy", **MARCH)
    assert [c.item_id for c in result.cards] == [small]
    assert by_id(result)[small].matched == MATCHED_BOTH   # the chunk stream reached it, after the widening
    assert by_id(result)[small].window == "vacation policy for March"
    assert [limit for limit, _ in w.repo.chunk_limits] == [CHUNK_FETCH, FETCH_MAX]


# --- the exact-term stream (§24.6) ----------------------------------------------------------------------------

async def test_an_invoice_number_and_an_amount_written_differently_are_found(monkeypatch):
    w = world()
    invoice, decoy = w.drive.add("scan.pdf"), w.drive.add("calendar.pdf")
    await settle_metadata(w, monkeypatch)
    state = await w.repo.get_sync_state("u")
    w.repo.states["u"] = type(state)(**{**state.__dict__, "applied_space": "another-space/8"})   # vectors off
    await index(w, invoice, ["Supplier: Acme S.L.", "Factura INV-2024/0012 total 1 250,00 €", "Thank you"],
                [FAR, FAR, FAR])
    await index(w, decoy, ["Holidays 2024"], [FAR])
    by_number = await w.search("invoice inv 2024-0012")
    # review D-M1: the query holds only the maximal form of the number; its year fragment no longer finds the decoy
    assert [c.item_id for c in by_number.cards] == [invoice] and decoy not in by_id(by_number)
    hit = by_id(by_number)[invoice]
    assert hit.matched == MATCHED_CONTENT and "INV-2024/0012" in hit.window
    assert w.repo.chunk_limits == []   # refreshing: the vector streams are off, the term stream still runs
    by_amount = await w.search("1250.00")
    assert [c.item_id for c in by_amount.cards] == [invoice]
    assert "1 250,00 €" in by_amount.cards[0].window
    assert "exact numbers and codes" in render_results(by_amount)


async def test_one_array_contains_query_per_term_at_most_the_cap(monkeypatch):
    w = world()
    w.drive.add("a.pdf")
    await settle_metadata(w, monkeypatch)
    query = " ".join(f"ID-{n:04d}77" for n in range(CONTENT_TERM_QUERIES + 4))
    await w.search(query, kind="pdf")
    assert len(w.repo.term_calls) == CONTENT_TERM_QUERIES
    assert all(kind == "pdf" for _, _, kind in w.repo.term_calls)
    w.repo.term_calls.clear()
    await w.search("INV-2024/0012", kind="spreadsheet")   # a family: no kind on the query, a post-filter
    assert {kind for _, _, kind in w.repo.term_calls} == {None}


async def test_the_widening_rereads_only_terms_whose_page_was_full(monkeypatch):
    monkeypatch.setattr(search_mod, "CONTENT_TERM_FETCH", 2)
    w = world()
    docs = [w.drive.add(f"d{i}.pdf") for i in range(3)]
    await settle_metadata(w, monkeypatch)
    await index(w, docs[0], ["year 2024 report code 98765"], [FAR])
    for doc in docs[1:]:
        await index(w, doc, ["year 2024 report"], [FAR])
    await w.search("2024 98765", **MARCH)   # the filter leaves nothing: the term page of "2024" was full
    terms = [(t, limit) for t, limit, _ in w.repo.term_calls]
    assert ("2024", 2) in terms and ("2024", FETCH_MAX) in terms
    assert terms.count(("98765", 2)) == 1 and ("98765", FETCH_MAX) not in terms


# --- folder scope (§10.1 Folder) -------------------------------------------------------------------------------

async def test_folder_scope_finds_a_weakly_named_file_deep_in_the_subtree_by_content(monkeypatch):
    w = world(page_size=500)
    work = w.drive.add("Work", folder=True)
    level = work
    for name in ("2024", "Q3", "misc"):
        level = w.drive.add(name, parent=level, folder=True)
    target = w.drive.add("IMG_0001.pdf", parent=level)
    decoys = [w.drive.add(f"note-{i}.pdf", parent=work) for i in range(60)]
    outside = [w.drive.add(f"other-{i}.pdf", parent=ROOT) for i in range(3)]
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "boiler warranty")
    for item in decoys:
        w.repo.vectors[("u", item)] = list(q)   # 60 nearer cards: the target's card is outside the card page
    w.repo.vectors[("u", target)] = FAR
    await index(w, target, ["the boiler warranty lasts five years"], [[x * 0.6 + 0.05 for x in q]])
    for item in outside:   # 1100 nearer chunks elsewhere: the user-wide chunk kNN never reaches the target
        await index(w, item, ["unrelated"] * 367, [q] * 367)
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    wide = await w.search("boiler warranty")
    assert target not in by_id(wide) or by_id(wide)[target].matched != MATCHED_CONTENT
    scoped = await w.search("boiler warranty", folder="Work")
    assert scoped.cards[0].item_id == target and scoped.cards[0].matched == MATCHED_CONTENT
    assert scoped.cards[0].path == "Work/2024/Q3/misc/IMG_0001.pdf"
    line = log.info.call_args[0][0]
    assert "scope=folder-memory" in line and "content=memory" in line and "chunk_kept=1" in line


async def test_a_folder_over_the_in_memory_chunk_bound_uses_the_wide_streams_with_the_folder_filter(monkeypatch):
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    w = world()
    work = w.drive.add("Work", folder=True)
    inside = w.drive.add("in.pdf", parent=work)
    outside = w.drive.add("out.pdf")
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "lease")
    await index(w, inside, ["lease one", "lease two", "lease three"], [q, q, q])
    await index(w, outside, ["lease elsewhere"], [q])
    result = await w.search("lease", folder="Work")
    assert [c.item_id for c in result.cards] == [inside]
    assert w.repo.chunk_limits == [(CHUNK_FETCH, None)]


# --- the summary line ----------------------------------------------------------------------------------------

async def test_the_summary_line_counts_the_content_streams(monkeypatch):
    w = world()
    doc = w.drive.add("scan.pdf")
    await settle_metadata(w, monkeypatch)
    await index(w, doc, ["Invoice INV-2024/0012", "page two"], [FAR, FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    await w.search("INV-2024/0012")
    line = log.info.call_args[0][0]
    # review D-M1: "INV-2024/0012" is one maximal query term (its fragments are index-side only)
    for part in ("content=wide", "chunk=2", "chunk_kept=1", "term_queries=1", "term_kept=1", "windows=1"):
        assert part in line, (part, line)


# --- the window budget (WINDOW_CHARS_TOTAL) --------------------------------------------------------------------

async def test_eight_content_hits_of_three_full_chunks_fit_the_window_budget(monkeypatch):
    from src.domain.drive_index_params import CHUNK_CHARS, K, WINDOW_CHARS_TOTAL
    w = world()
    docs = [w.drive.add(f"scan-{n}.pdf") for n in range(K)]
    await settle_metadata(w, monkeypatch)
    q = await query_vector(w, "warranty terms")
    for n, item in enumerate(docs):
        texts = [(f"d{n}-c{i} " + "y" * CHUNK_CHARS)[:CHUNK_CHARS] for i in range(3)]
        await index(w, item, texts, [FAR, [x * (1 + n / 100) for x in q], FAR])
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    result = await w.search("warranty terms")
    windows = [c.window for c in result.cards]
    assert len(windows) == K and all(windows)
    assert sum(len(t) for t in windows) <= WINDOW_CHARS_TOTAL
    assert all(c.window_shortened for c in result.cards)
    assert all(c.window.startswith(f"d{docs.index(c.item_id)}-c1") for c in result.cards)   # the best chunk
    assert "matching passage (shortened):" in render_results(result)
    assert f"window_chars={sum(len(t) for t in windows)}" in log.info.call_args[0][0]
