"""Filters applied to a capped candidate set say so (USER_DRIVE_INDEXING_RFC §10.1 steps 4-5, §10.2).

A query with a date, folder or kind-family filter ranks the streams' top pages and filters those. When a page came
back full (after the one widening) and the filters dropped candidates, a file that matches may sit past the page:
the answer hedges and never says a flat "No matching files or folders.". A kind family runs one card kNN per member
kind, so the kind filter itself never drops a card candidate."""
from datetime import datetime, timezone

from src.domain.drive_search import DriveSearchRequest, render_results
from src.services import drive_search_service as search_mod

from .test_drive_search_service import SpyRepo, World, paths, taken

MARCH = dict(date_from=datetime(2026, 3, 1, tzinfo=timezone.utc), date_to=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))


def _small_pages(monkeypatch, fetch=3, widened=4):
    monkeypatch.setattr(search_mod, "NAME_FETCH", fetch)
    monkeypatch.setattr(search_mod, "CARD_FETCH", fetch)
    monkeypatch.setattr(search_mod, "FETCH_MAX", widened)


def _query_far_from(w, target_word):
    """The query vector sits on every card except the target's: the card kNN never reaches the target."""
    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if target_word in text:
            return [0.0, 1.0] + [0.0] * 6
        return [1.0, 0.01] + [0.0] * 6
    w.emb.get_embedding = emb


class KindSpy(SpyRepo):
    def __init__(self):
        super().__init__()
        self.card_kinds = []

    async def find_nearest_cards(self, user_id, vector, limit, kind=None):
        self.card_kinds.append(kind)
        return await super().find_nearest_cards(user_id, vector, limit, kind)


def _world(**kw):
    w = World(**kw)
    spy = KindSpy()
    w.repo = spy
    w.indexer._repo = spy
    w.search_svc._repo = spy
    return w


async def test_date_filter_over_full_pages_with_no_survivor_hedges(monkeypatch):
    w = World(page_size=10_000)
    _query_far_from(w, "zz-invoice")
    for n in range(8):
        w.drive.add(f"invoice-{n}.pdf")   # dated 2026-10-11, lower ids, share the key
    target = w.drive.add("zz-invoice.pdf")
    await w.settle()
    w.set_state(target, effective_date=datetime(2026, 3, 5, tzinfo=timezone.utc))
    _small_pages(monkeypatch)
    result = await w.search("invoice", **MARCH)
    text = render_results(result)
    assert result.cards == () and result.capped
    assert "No matching files or folders." not in text
    assert "does not mean" in text and "drive_query empty" in text


async def test_date_filter_over_full_pages_with_survivors_hedges(monkeypatch):
    w = World(page_size=10_000)
    for n in range(8):
        w.drive.add(f"invoice-{n}.pdf", facets=taken(f"2026-03-0{n + 1}"))
    for n in range(8):
        w.drive.add(f"invoice-old-{n}.pdf", facets=taken(f"2025-01-0{n + 1}"))
    await w.settle()
    _small_pages(monkeypatch)
    result = await w.search("invoice", **MARCH)
    text = render_results(result)
    assert result.cards and result.capped and result.candidates > 0
    assert f"best {result.candidates}" in text and "more files may match" in text


async def test_no_hedge_when_the_pages_were_not_full():
    w = World()
    w.drive.add("invoice-1.pdf")
    w.drive.add("invoice-2.pdf")
    await w.settle()
    result = await w.search("invoice", **MARCH)
    text = render_results(result)
    assert result.cards == () and not result.capped
    assert "No matching files or folders." in text and "more files may match" not in text


async def test_no_hedge_when_no_filter_dropped_a_candidate(monkeypatch):
    w = World(page_size=10_000)
    for n in range(8):
        w.drive.add(f"invoice-{n}.pdf")
    await w.settle()
    _small_pages(monkeypatch)
    result = await w.search("invoice")
    assert result.cards and not result.capped
    assert "more files may match" not in render_results(result)


async def test_a_kind_family_runs_one_card_knn_per_member_kind(monkeypatch):
    w = _world(page_size=10_000)
    _query_far_from(w, "zz-invoice")   # the user-wide kNN would return only the PDFs
    for n in range(8):
        w.drive.add(f"invoice-{n}.pdf")
    w.drive.add("zz-invoice.xlsx")
    await w.settle()
    _small_pages(monkeypatch)
    result = await w.search("invoice", kind="spreadsheet")
    assert paths(result) == ["zz-invoice.xlsx"]
    assert sorted(set(w.repo.card_kinds)) == ["csv", "ods", "xls", "xlsx"]


async def test_one_kind_is_still_one_prefiltered_knn():
    w = _world()
    w.drive.add("a.pdf")
    await w.settle()
    await w.search_svc.search("u", DriveSearchRequest(query="a", kind="pdf"))
    assert w.repo.card_kinds == ["pdf"]


async def test_a_family_merges_its_member_knn_pages_by_distance():
    """The nearest card is an xlsx added last (the highest item id, the alphabetically later kind): the merged
    card stream puts it first. No name shares a key with the query, so the card stream alone orders the result."""
    w = _world()

    async def emb(text, task_type="RETRIEVAL_DOCUMENT"):
        w.emb.calls.append(text)
        if task_type == "RETRIEVAL_QUERY":
            return [1.0] + [0.0] * 7
        if "target" in text:
            return [1.0, 0.05] + [0.0] * 6
        if "near" in text:
            return [1.0, 0.5] + [0.0] * 6
        return [0.2, 1.0] + [0.0] * 6
    w.emb.get_embedding = emb
    w.drive.add("far one.csv")
    w.drive.add("near two.csv")
    w.drive.add("far three.ods")
    w.drive.add("target four.xlsx")
    await w.settle()
    result = await w.search("quarterly", kind="spreadsheet")
    assert w.repo.name_keys and not any(c.name.startswith("quarterly") for c in result.cards)
    assert [c.name for c in result.cards][:2] == ["target four.xlsx", "near two.csv"]
