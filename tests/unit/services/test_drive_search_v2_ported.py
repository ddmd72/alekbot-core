"""DriveSearchService, Search v2 (USER_DRIVE_INDEXING_RFC §25.5): the v1 search tests' still-valid properties, ported
to a v2 index (`V2World`, a state rebuilt under v2), and the v2 tests that replace v1 mechanics the spec removed
(RRF and cover-first, always-8, term selectivity and its probes, widening). Each test names the v1 test it ports or
replaces. Two ports are strict xfails: the property still holds by spec, the code does not meet it yet (review
rv_sv2_p6 M1)."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.domain.drive_enrichment import DriveProfile
from src.domain.drive_index_params import (
    CARD_FETCH, CHUNK_CHARS, CHUNK_FETCH, MAX_CONCEPTS, MENTIONED_SHOWN, WINDOW_CHARS_TOTAL_V2, WINDOW_MAX,
)
from src.domain.drive_keys import query_concepts
from src.domain.drive_search import render_results
from src.services import drive_search_service as search_mod

from .drive_search_v2_fixture import KeySpyRepo, V2World, card, tier_of, vec

MARCH = dict(date_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
             date_to=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))
EXACT = ("about", "mentioned", "translated", "labelled")
# A synthetic IBAN-shaped string (no real account), assembled so no source line holds it whole.
IBAN = " ".join(("ZZ07", "0000", "1111", "2222", "3333", "44"))


class Spy(KeySpyRepo):
    """KeySpyRepo plus the reads the v1 tests pinned: kNN limits and kinds, name pages, ranking reads, record reads."""

    def __init__(self) -> None:
        super().__init__()
        self.card_calls = []
        self.chunk_calls = []
        self.ranking_reads = []
        self.record_reads = []

    async def find_nearest_cards(self, user_id, vector, limit, kind=None):
        self.card_calls.append((limit, kind))
        return await super().find_nearest_cards(user_id, vector, limit, kind)

    async def find_nearest_chunks(self, user_id, vector, limit, kind=None):
        self.chunk_calls.append((limit, kind))
        return await super().find_nearest_chunks(user_id, vector, limit, kind)

    async def get_chunks_with_vectors(self, user_id, item_id, generation, indexes, **flags):
        self.ranking_reads.append(flags)
        return await super().get_chunks_with_vectors(user_id, item_id, generation, indexes, **flags)

    async def get_records(self, user_id, item_ids):
        self.record_reads.append(tuple(item_ids))
        return await super().get_records(user_id, item_ids)


def world() -> V2World:
    w = V2World()
    spy = Spy()
    spy.records, spy.vectors, spy.chunks, spy.hints, spy.states = (w.repo.records, w.repo.vectors, w.repo.chunks,
                                                                   w.repo.hints, w.repo.states)
    w.repo = spy
    w.service._repo = spy
    return w


def tiers(result, *wanted):
    return [c.item_id for c in result.cards if c.tier in (wanted or EXACT)]


def vectors_off(w):
    from dataclasses import replace
    w.repo.states["u"] = replace(w.repo.states["u"], applied_space="another-space/8")


def pin_chunk(w, item, index, vector, generation=1):
    chunk, _ = w.repo.chunks[("u", item, generation, index)]
    w.repo.chunks[("u", item, generation, index)] = (chunk, list(vector))


def log_line(monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(search_mod, "logger", log)
    return log


# -- from test_drive_search_service.py ------------------------------------------------------------------------------

async def test_a_name_match_is_about_and_nearest_cards_are_only_related():
    """Replaces TestRanking.test_a_record_in_both_streams_outranks_one_in_a_single_stream and
    TestScale.test_a_query_no_name_covers_keeps_plain_rrf (RRF and cover-first are gone, §25.9): the file whose name
    holds the word is About, ahead of anything; files near the query only by meaning are Related, at most three."""
    w = world()
    target, other = vec("target"), vec("other")
    for i in range(30):
        w.file(f"misc{i}.pdf", card_vector=[x + 0.02 * ((i * 7 + j) % 5) for j, x in enumerate(other)])
    near = [w.file(f"memo{i}.pdf", card_vector=[x + 0.001 * i for x in target]) for i in range(5)]
    invoice = w.file("invoice march.pdf")
    w.emb.pinned["invoice"] = target
    result = await w.search("invoice")
    assert result.cards[0].item_id == invoice and tiers(result) == [invoice]
    related = tiers(result, "related")
    assert 0 < len(related) <= 3 and set(related) <= set(near)


async def test_the_card_knn_runs_once_at_card_fetch():
    """Ports TestRanking.test_no_widening_when_the_streams_were_not_full: one card kNN at CARD_FETCH, no second
    (v2 has no widening)."""
    w = world()
    w.file("lease.pdf")
    await w.search("lease")
    assert w.repo.card_calls == [(CARD_FETCH, None)]


async def test_one_summary_line(monkeypatch):
    """Ports TestRanking.test_one_summary_line to the v2 fields (§25.5.7)."""
    w = world()
    w.file("lease.pdf")
    log = log_line(monkeypatch)
    await w.search("lease")
    assert log.info.call_count == 1
    line = log.info.call_args[0][0]
    for part in ("card=", "name=", "concepts=1", "about=1", "visible=", "filtered=", "returned=1"):
        assert part in line, part
    assert "fused=" not in line and "name_kept=" not in line


async def test_a_kind_filter_prefilters_the_card_knn_and_keeps_only_that_kind():
    """Ports TestFilters.test_kind_prefilter_reaches_both_streams and test_one_kind_is_still_one_prefiltered_knn:
    a single kind prefilters the kNN (§25.5.5); the name stream is user-wide by design (df, §25.5.2)."""
    w = world()
    w.file("report.pdf")
    xlsx = w.file("report.xlsx", kind="xlsx")
    result = await w.search("report", kind="xlsx")
    assert tiers(result) == [xlsx] and [c.item_id for c in result.cards] == [xlsx]
    assert w.repo.card_calls == [(CARD_FETCH, "xlsx")]


async def test_kind_filter_finds_a_name_match_past_a_full_user_wide_page(monkeypatch):
    """The other half of TestFilters.test_kind_prefilter_reaches_both_streams: a filtered search sees its file even
    when the unfiltered name page is full of other kinds, and never says no file in scope holds the word."""
    monkeypatch.setattr(search_mod, "NAME_FETCH", 5)
    w = world()
    for i in range(8):
        w.file(f"report {i}.pdf")
    xlsx = w.file("report.xlsx", kind="xlsx", item="Z999")
    result = await w.search("report", kind="xlsx")
    assert tier_of(result, xlsx) == "about"
    assert "No file in this scope holds" not in render_results(result)


async def test_large_subtree_falls_back_to_user_wide_streams_filtered_by_chain(monkeypatch):
    """Ports TestFolderScope.test_large_subtree_falls_back_to_user_wide_streams_filtered_by_chain; its "five cards"
    was the always-fill of v1 (§25.9) and is not ported."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 2)
    w = world()
    work = w.folder("Work")
    alpha = w.folder("Alpha", parent=work)
    deep = w.folder("a", parent=w.folder("b", parent=alpha))
    x17 = w.file("x17.bin", kind="other", parent=deep)
    w.file("x17.bin", kind="other", item="OUT1")   # same name outside the folder
    log = log_line(monkeypatch)
    result = await w.search("x17", folder="Work")
    assert result.cards[0].item_id == x17 and result.cards[0].path == "Work/Alpha/b/a/x17.bin"
    assert all(c.path.startswith("Work/") for c in result.cards)
    assert w.repo.card_calls == [(CARD_FETCH, None)] and "scope=folder-wide" in log.info.call_args[0][0]


async def test_a_date_filtered_query_reads_the_filtered_about_page_past_a_full_page(monkeypatch):
    """Replaces TestRanking.test_widens_once_at_the_cap_when_filters_leave_fewer_than_k (widening is gone): past a
    full about page under a date filter, the about stream reads the filtered composite, so an old match is found."""
    monkeypatch.setattr(search_mod, "ABOUT_FETCH", 3)
    monkeypatch.setattr(search_mod, "GROUP_READ_MAX", 3)
    w = world()
    for i in range(8):
        w.file(f"IMG_{i:04d}.jpg", kind="image", taken=f"2025-05-{i + 1:02d}", place_forms=("Coimbra",))
    old = w.file("IMG_9999.jpg", kind="image", taken="2020-05-05", place_forms=("Coimbra",))
    result = await w.search("coimbra", date_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
                            date_to=datetime(2020, 12, 31, tzinfo=timezone.utc))
    assert tiers(result) == [old]


async def test_reads_through_full_about_pages_are_bounded(monkeypatch):
    """Replaces test_drive_search_pools_folders_widening TestSearchDecisions.
    test_ranked_widens_at_most_once_even_when_the_widened_page_is_full: every page full, the about stream still stops
    at GROUP_READ_MAX (one filtered read past it), so the search terminates with a bounded number of reads."""
    monkeypatch.setattr(search_mod, "ABOUT_FETCH", 2)
    monkeypatch.setattr(search_mod, "GROUP_READ_MAX", 4)
    w = world()
    for i in range(20):
        w.file(f"IMG_{i:04d}.jpg", kind="image", taken=f"2025-05-{i + 1:02d}", place_forms=("Coimbra",))
    result = await w.search("coimbra", **MARCH)
    about_reads = [n for stream, n in w.repo.key_lists if stream == "about"]
    assert len(about_reads) == 4 // 2 + 1 and tiers(result) == []
    # the one filtered read past the full pages came back short: the scope's absence is read, not guessed
    assert 'No file in this scope holds "coimbra".' in render_results(result)


# -- from test_drive_search_capped_filters.py -----------------------------------------------------------------------

async def test_date_filter_over_full_pages_never_claims_absence(monkeypatch):
    """Ports test_date_filter_over_full_pages_with_no_survivor_hedges: when the user-wide page was full and the date
    filter left nothing of it, the answer hedges — never a flat negative."""
    monkeypatch.setattr(search_mod, "NAME_FETCH", 3)
    w = world()
    for n in range(8):
        w.file(f"invoice-{n}.pdf", effective="2026-10-11")
    w.file("zz-invoice.pdf", effective="2026-03-05", item="Z999")
    text = render_results(await w.search("invoice", **MARCH))
    assert "No file in this scope holds" not in text and "No matching files" not in text
    assert "a candidate page was full, so more files may match" in text


async def test_date_filter_over_full_pages_with_survivors_hedges(monkeypatch):
    """Ports test_date_filter_over_full_pages_with_survivors_hedges: survivors are shown, the count is a lower bound
    and the coverage line says more files may match. Also replaces test_no_hedge_when_no_filter_dropped_a_candidate:
    v1's filter-only hedge became the coverage line's full-page part (§25.5.6), honest with or without a filter."""
    monkeypatch.setattr(search_mod, "NAME_FETCH", 3)
    w = world()
    march = [w.file(f"invoice-{n}.pdf", effective=f"2026-03-0{n + 1}") for n in range(8)]
    for n in range(8):
        w.file(f"invoice-old-{n}.pdf", effective=f"2025-01-0{n + 1}")
    result = await w.search("invoice", **MARCH)
    text = render_results(result)
    assert tiers(result) and set(tiers(result)) <= set(march)
    assert "of at least" in text and "a candidate page was full, so more files may match" in text


async def test_a_kind_family_runs_one_card_knn_per_member_kind():
    """Ports test_a_kind_family_runs_one_card_knn_per_member_kind (RFC Δ37, §25.5.5)."""
    w = world()
    for n in range(8):
        w.file(f"invoice-{n}.pdf")
    xlsx = w.file("zz-invoice.xlsx", kind="xlsx")
    result = await w.search("invoice", kind="spreadsheet")
    assert [c.item_id for c in result.cards if c.tier != "related"] == [xlsx]
    assert sorted(kind for _, kind in w.repo.card_calls) == ["csv", "ods", "xls", "xlsx"]


async def test_a_family_merges_its_member_knn_pages_into_one_meaning_pool():
    """Ports test_a_family_merges_its_member_knn_pages_by_distance: the nearest card, an xlsx added last among
    csv/xls cards, leads the meaning tier, so the member pages are merged by distance, not by kind or id."""
    w = world()
    other, target = vec("other"), vec("target")
    for i in range(14):
        w.file(f"sheet{i}.csv" if i % 2 else f"sheet{i}.xls", kind="csv" if i % 2 else "xls",
               card_vector=[x + 0.03 * ((i * 5 + j) % 7) for j, x in enumerate(other)])
    nearest = w.file("zz.xlsx", kind="xlsx", card_vector=list(target))
    w.emb.pinned["zebra"] = target
    result = await w.search("zebra", kind="spreadsheet")
    assert tiers(result, "related")[:1] == [nearest]


# -- from test_drive_search_content_streams.py ----------------------------------------------------------------------

async def test_a_weakly_named_scan_is_found_by_its_content_with_a_passage():
    """Ports test_a_weakly_named_scan_is_found_by_its_content_with_a_window: the words in one passage make it
    Mentioned (exact), its passage read by id."""
    w = world()
    scan = w.file("Scan_0012.pdf", chunks=["1. Parties. ", "7.2 The notice period is 30 days for either party. ",
                                           "8. Law."])
    result = await w.search("notice period")
    assert tier_of(result, scan) == "mentioned" and result.envelope.content_search
    assert "The notice period is 30 days" in card(result, scan).passage
    assert card(result, scan).sources == ('words "notice", "period" in one passage',)


async def test_reindex_to_fewer_chunks_never_serves_the_old_generation():
    """Ports test_reindex_at_the_same_ctag_to_fewer_chunks_never_serves_the_old_generation (§7.4 fence)."""
    w = world()
    doc = w.file("contract.pdf", chunks=["old one", "old two", "OLD PENALTY CLAUSE"], generation=1)
    w._chunks(doc, "pdf", ["new single chunk"], {}, 2)
    w.set(doc, indexed_generation=2, index_generation=2, chunk_count=1)
    old = await w.search("penalty")
    assert tiers(old) == [] and "OLD" not in render_results(old) and 'No file holds "penalty".' in render_results(old)
    new = await w.search("single")
    assert tier_of(new, doc) == "mentioned" and card(new, doc).passage == "new single chunk"


async def test_a_crash_after_n_chunk_writes_serves_only_the_previous_generation():
    """Ports test_a_crash_after_n_chunk_writes_serves_only_the_previous_generation (§7.4, §8.1 step 2)."""
    w = world()
    doc = w.file("contract.pdf", chunks=["v1 text with the penalty", "v1 text b"], generation=1)
    w._chunks(doc, "pdf", ["v2 partial PENALTY", "v2 partial two"], {}, 2)   # written, never made readable
    w.set(doc, index_generation=2)
    result = await w.search("penalty")
    assert tier_of(result, doc) == "mentioned" and card(result, doc).passage == "v1 text with the penalty"
    assert "v2 partial" not in render_results(result)


async def test_a_file_with_many_matching_chunks_is_one_card():
    """Replaces test_many_hits_of_one_document_count_once_in_the_fusion (RRF pooling is gone): each file is shown
    once, in its best tier, however many of its chunks hold the words (§25.5.4)."""
    w = world()
    many = w.file("notes.pdf", chunks=[f"budget part {i}" for i in range(5)])
    one = w.file("memo.pdf", chunks=["the budget memo"])
    result = await w.search("budget")
    assert sorted(tiers(result)) == sorted([many, one])
    assert len(result.cards) == len({c.item_id for c in result.cards})
    assert "Mentioned in (2):" in render_results(result)


async def test_a_400_chunk_document_does_not_starve_others_under_a_filter():
    """Ports test_a_400_chunk_document_does_not_starve_others_under_a_filter: the word stream reads on past a page
    full of one document's chunks (§25.5.2), so the small dated file is found under the date filter."""
    w = world()
    w.file("handbook.pdf", effective="2026-10-01", chunks=[f"vacation policy section {i}" for i in range(400)])
    small = w.file("march-note.pdf", effective="2026-03-05", chunks=["vacation policy for March"])
    result = await w.search("vacation policy", **MARCH)
    assert [c.item_id for c in result.cards if c.tier != "related"] == [small]
    assert card(result, small).passage == "vacation policy for March"


async def test_an_invoice_number_and_an_amount_written_differently_are_found():
    """Ports test_an_invoice_number_and_an_amount_written_differently_are_found: number concepts in their maximal
    forms (§25.5.1 step 3) find the invoice while the vectors are off; the year fragment does not find the decoy."""
    w = world()
    vectors_off(w)
    invoice = w.file("scan.pdf", chunks=["Supplier: Acme S.L.", "Factura INV-2024/0012 total 1 250,00 €",
                                         "Thank you"])
    decoy = w.file("calendar.pdf", chunks=["Holidays 2024"])
    by_number = await w.search("INV-2024/0012")
    assert tiers(by_number) == [invoice] and decoy not in [c.item_id for c in by_number.cards]
    assert "INV-2024/0012" in card(by_number, invoice).passage and w.repo.chunk_calls == []
    by_amount = await w.search("1250.00")
    assert tiers(by_amount) == [invoice] and "1 250,00 €" in card(by_amount, invoice).passage


async def test_words_past_max_concepts_are_cut_and_named():
    """Replaces test_one_array_contains_query_per_term_at_most_the_cap (CONTENT_TERM_QUERIES is retired): at most
    MAX_CONCEPTS concepts, the rest named in the result (§25.5.1 step 6); every fetch's ≤ 30 keys is
    TestKeysAndLog.test_every_fetch_takes_at_most_thirty_keys."""
    w = world()
    w.file("a.pdf", chunks=["alpha"])
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
    text = render_results(await w.search(" ".join(words)))
    assert f'words past the first ones were not searched: "{words[MAX_CONCEPTS]}", "{words[MAX_CONCEPTS + 1]}"' in text


async def test_the_word_stream_reads_on_past_a_full_page(monkeypatch):
    """Replaces test_the_widening_rereads_only_terms_whose_page_was_full (widening is gone): a full word page is
    continued after its last item (§25.5.2), so files past the first page are found."""
    monkeypatch.setattr(search_mod, "WORD_FETCH", 2)
    w = world()
    files = [w.file(f"d{i}.pdf", chunks=["the lighthouse keeper", "lighthouse again"]) for i in range(3)]
    result = await w.search("lighthouse")
    assert sorted(tiers(result)) == sorted(files)
    assert sum(1 for stream, _ in w.repo.key_lists if stream == "word") >= 3


async def test_folder_scope_finds_a_weakly_named_file_deep_in_the_subtree_by_content(monkeypatch):
    """Ports test_folder_scope_finds_a_weakly_named_file_deep_in_the_subtree_by_content. Its "the wide search misses
    it" half was a v1 kNN limit; v2's word stream is exhaustive, so only the scoped half is ported."""
    w = world()
    work = w.folder("Work")
    level = work
    for name in ("2024", "Q3", "misc"):
        level = w.folder(name, parent=level)
    target = w.file("IMG_0001.pdf", parent=level, chunks=["the boiler warranty lasts five years"])
    for i in range(10):
        w.file(f"note-{i}.pdf", parent=work, chunks=["unrelated"])
    w.file("other.pdf", chunks=["the boiler warranty of the neighbours"])
    log = log_line(monkeypatch)
    scoped = await w.search("boiler warranty", folder="Work")
    assert tiers(scoped) == [target] and card(scoped, target).path == "Work/2024/Q3/misc/IMG_0001.pdf"
    line = log.info.call_args[0][0]
    assert "scope=folder-memory" in line and "content=memory" in line


async def test_a_folder_over_the_in_memory_chunk_bound_uses_the_wide_streams_with_the_folder_filter(monkeypatch):
    """Ports the test of the same name (§25.5.5)."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    w = world()
    work = w.folder("Work")
    inside = w.file("in.pdf", parent=work, chunks=["lease one", "lease two", "lease three"])
    w.file("out.pdf", chunks=["lease elsewhere"])
    log = log_line(monkeypatch)
    result = await w.search("lease", folder="Work")
    assert [c.item_id for c in result.cards if c.tier != "related"] == [inside]
    assert w.repo.chunk_calls == [(CHUNK_FETCH, None)] and "content=wide" in log.info.call_args[0][0]


async def test_a_truncated_word_page_in_the_wide_folder_branch_never_states_the_folder_lacks_the_word(monkeypatch):
    """The wide folder branch (chunks over FOLDER_INMEM_CHUNKS) reads the user-wide word pages. A full page that stops
    at a file outside the folder has not reached the folder's files, so "No file in this scope holds ..." must not
    be stated (P6 review M1); the word is reported as not checked instead."""
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CHUNKS", 2)
    monkeypatch.setattr(search_mod, "WORD_FILES_MAX", 1)
    monkeypatch.setattr(search_mod, "WORD_FETCH", 1)
    w = world()
    w.file("aaa-notes.txt", chunks=["the lighthouse keeper"])   # outside the folder, sorts first by item_id
    library = w.folder("Library")
    w.file("novel.epub", parent=library, chunks=["the lighthouse keeper", "chapter two", "chapter three"])
    result = await w.search("lighthouse", folder="Library")
    text = render_results(result)
    assert 'No file in this scope holds "lighthouse".' not in text
    assert 'could not be checked in the whole scope: "lighthouse"' in text


async def test_the_summary_line_counts_the_content_streams(monkeypatch):
    """Ports test_the_summary_line_counts_the_content_streams to the v2 fields (§25.5.7)."""
    w = world()
    w.file("scan.pdf", chunks=["Invoice INV-2024/0012", "page two"])
    log = log_line(monkeypatch)
    await w.search("INV-2024/0012")
    line = log.info.call_args[0][0]
    for part in ("content=wide", "concepts=1", "word_queries=1", "mentioned=1", "chunk=2", "tested=1"):
        assert part in line, (part, line)


@pytest.mark.slow
async def test_mentioned_passages_share_the_window_budget():
    """Ports test_eight_content_hits_of_three_full_chunks_fit_the_window_budget to v2's budget (§25.5.6): each
    passage at most WINDOW_MAX, all at most WINDOW_CHARS_TOTAL_V2, each cut from the chunk holding the words."""
    w = world()
    docs = []
    for n in range(8):
        texts = [("filler " * 400)[:CHUNK_CHARS],
                 (f"d{n} " + "y " * 500 + "the warranty terms apply " + "z " * 500)[:CHUNK_CHARS],
                 ("filler " * 400)[:CHUNK_CHARS]]
        docs.append(w.file(f"scan-{n}.pdf", chunks=texts))
    result = await w.search("warranty terms")
    passages = [c.passage for c in result.cards if c.tier == "mentioned"]
    assert len(passages) == MENTIONED_SHOWN and all(passages)
    assert all(len(p) <= WINDOW_MAX and "warranty terms" in p for p in passages)
    assert sum(len(p) for p in passages) <= WINDOW_CHARS_TOTAL_V2
    assert f"Mentioned in (showing {MENTIONED_SHOWN} of 8):" in render_results(result)


# -- from test_drive_search_folder_memory_reads.py ------------------------------------------------------------------

async def test_folder_ranking_reads_word_keys_and_vectors_never_text():
    """Ports test_ranking_with_a_vector_reads_no_text_and_no_terms_for_a_query_without_terms and
    test_a_query_with_terms_reads_the_terms: every v2 concept is tested on the chunks' word keys, so the in-memory
    ranking reads them (and the vectors, with a query vector) but never chunk text; the passage reads text by id."""
    w = world()
    work = w.folder("Work")
    doc = w.file("scan.pdf", parent=work, chunks=["the boiler warranty lasts five years"])
    result = await w.search("boiler warranty", folder="Work")
    assert card(result, doc).passage == "the boiler warranty lasts five years"
    assert w.repo.ranking_reads == [{"text": False, "terms": True, "vectors": True}]


async def test_folder_ranking_while_refreshing_reads_word_keys_but_no_vectors():
    """Ports test_refreshing_with_terms_reads_terms_but_no_vectors."""
    w = world()
    vectors_off(w)
    work = w.folder("Work")
    doc = w.file("scan.pdf", parent=work, chunks=["invoice 98765"])
    result = await w.search("invoice 98765", folder="Work")
    assert tiers(result) == [doc]
    assert w.repo.ranking_reads == [{"text": False, "terms": True, "vectors": False}]


# -- from test_drive_search_matched_shown.py ------------------------------------------------------------------------

async def test_a_nearest_only_content_hit_is_related_with_no_passage_and_no_match_line():
    """Ports test_a_nearest_only_content_hit_is_labelled_nearest_passage (F5-8): a file found only by meaning is
    never presented as a match — it is Related, with no passage and no "matched:" line (§25.5.3)."""
    w = world()
    scan = w.file("Scan_0012.pdf", chunks=["7.2 Either party may terminate on 30 days notice."])
    q = vec("q:cancellation clause")
    pin_chunk(w, scan, 0, q)
    w.repo.vectors[("u", scan)] = list(q)
    result = await w.search("cancellation clause")
    assert tier_of(result, scan) == "related" and card(result, scan).passage == ""
    text = render_results(result)
    assert "matched:" not in text and "passage:" not in text and "Related by meaning only, unverified" in text


async def test_an_exact_word_hit_is_a_passage_match():
    """Ports test_an_exact_term_hit_is_a_matching_passage."""
    w = world()
    invoice = w.file("Scan_0099.pdf", chunks=["Factura INV-2024/0012 total 1 250,00 €"])
    text = render_results(await w.search("INV-2024/0012"))
    assert "Mentioned in (1):" in text and "passage: … Factura INV-2024/0012" in text
    assert 'matched: word "INV-2024/0012" in one passage' in text and f"drive:{invoice}" in text


# -- from test_drive_search_one_ancestry_load.py --------------------------------------------------------------------

async def test_every_stream_loads_its_ancestors_together():
    """Ports test_the_four_streams_load_their_ancestors_together: the ancestors of the about, name, word and card
    kNN streams' files load in one pass."""
    w = world()
    folders = [w.folder(f"F{n}") for n in range(4)]
    w.file("a.pdf", parent=folders[0], card_vector=list(vec("q:lease")))   # found by the card kNN only
    by_name = w.file("lease.pdf", parent=folders[1])
    by_chunk = w.file("b.pdf", parent=folders[2], chunks=["the lease terms"])
    by_about = w.file("c.pdf", parent=folders[3], profile=DriveProfile("Contrato", "A rental agreement", "es",
                                                                       "contract", label_words=("lease",)))
    result = await w.search("lease")
    assert {by_name, by_chunk, by_about} <= set(tiers(result))
    ancestor_reads = [ids for ids in w.repo.record_reads if set(ids) & set(folders)]
    assert len(ancestor_reads) == 1 and set(ancestor_reads[0]) == set(folders)


# -- term selectivity (test_drive_search_term_selectivity*.py), replaced by commonness by files --------------------

def _common_drive(w, parent=None):
    kw = {"parent": parent} if parent else {}
    for i in range(30):
        w.file(f"report-{i}.pdf", chunks=[f"annual figures for 2024, part {i}"], **kw)
    return w.file("boiler.pdf", chunks=["boiler service in 2024, invoice and figures attached"], **kw)


async def test_a_word_on_more_than_common_share_of_the_files_is_ignored_as_common_and_named():
    """Replaces test_a_year_on_many_documents_does_not_rank_them, test_a_year_on_30_files_is_dropped and the probe
    file's tests (term selectivity is gone, §25.9): a word held by more than COMMON_SHARE of the carded files may be
    missed and is named; the rarer word still decides (strict AND). A number is never common (P6 review m7, arbiter
    ruling: "receipts 2024" must not lose its year), so on the same drive "2024" is required, never ignored."""
    w = world()
    target = _common_drive(w)
    result = await w.search("boiler figures")
    assert tiers(result) == [target]
    assert tier_of(result, target) == "mentioned"
    assert '; ignored as common: "figures".' in render_results(result)
    year = await w.search("boiler 2024")
    assert tiers(year) == [target] and "ignored as common" not in render_results(year)
    alone = await w.search("2024 lighthouse")
    assert tiers(alone) == [] and 'No file holds "lighthouse".' in render_results(alone)


async def test_folder_in_memory_commonness_is_measured_on_the_subtree():
    """Replaces test_the_folder_in_memory_ranking_drops_a_term_on_too_many_chunks and
    test_folder_in_memory_a_year_on_30_files_is_dropped."""
    w = world()
    work = w.folder("Work")
    target = _common_drive(w, parent=work)
    result = await w.search("boiler 2024", folder="Work")
    assert tiers(result) == [target] and "Searched only within the requested folder" in render_results(result)


async def test_the_passage_is_cut_around_the_matched_words_not_the_nearest_chunk():
    """Replaces test_a_selective_term_anchors_the_window_on_its_chunk and
    test_a_concentrated_term_anchors_over_the_nearest_chunk: the passage comes from the chunk holding the words,
    even when another chunk of the file is nearer to the query vector."""
    w = world()
    doc = w.file("statement.pdf",
                 chunks=["opening balance and fees", "fees again", f"transfer to {IBAN} done"])
    pin_chunk(w, doc, 0, vec("q:" + IBAN))
    result = await w.search(IBAN)
    assert tier_of(result, doc) == "mentioned" and IBAN in card(result, doc).passage
    assert "opening balance" not in card(result, doc).passage


async def test_an_id_in_every_chunk_of_one_file_is_one_card_with_its_passage():
    """Replaces test_an_iban_repeated_in_every_chunk_of_a_statement_does_not_anchor_its_window and
    test_an_iban_in_120_chunks_of_one_contract_is_found_and_anchors_its_window."""
    w = world()
    doc = w.file("contract.pdf", chunks=[f"clause {i}: pay to {IBAN}" for i in range(120)])
    result = await w.search(IBAN)
    assert [c.item_id for c in result.cards] == [doc] and IBAN in result.cards[0].passage


async def test_folder_in_memory_an_id_in_one_file_is_found_with_its_passage():
    """Replaces test_folder_in_memory_an_iban_in_one_contract_is_found_and_anchors."""
    w = world()
    work = w.folder("Work")
    doc = w.file("contract.pdf", parent=work,
                 chunks=[f"clause {i}: pay to {IBAN}" for i in range(40)])
    result = await w.search(IBAN, folder="Work")
    assert tiers(result) == [doc] and IBAN in card(result, doc).passage


async def test_every_file_holding_a_word_is_counted_when_its_page_is_not_full():
    """Replaces test_a_term_whose_page_is_not_full_still_ranks_every_file_holding_it."""
    w = world()
    for i in range(12):
        w.file(f"log-{i}.pdf", chunks=[f"lighthouse visit {i}"])
    text = render_results(await w.search("lighthouse"))
    assert f"Mentioned in (showing {MENTIONED_SHOWN} of 12):" in text and "of at least" not in text


# -- test_drive_search_query_term_cap.py ----------------------------------------------------------------------------

def test_a_word_inside_the_query_cap_is_still_searched():
    """Ports test_a_term_inside_the_cap_is_still_found; the clip itself is
    test_drive_keys_query.test_the_query_is_clipped_at_a_word_boundary."""
    assert "98765" in [c.word for c in query_concepts("contract 98765 " + "words " * 400).concepts]
