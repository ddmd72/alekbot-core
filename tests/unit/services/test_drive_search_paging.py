"""`excerpt_range` page requests (`from N`): the PageSpan (USER_DRIVE_INDEXING_RFC §29.5 L10)."""
from __future__ import annotations

import pytest

from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_index_params import EXCERPTS_CHARS_MAX

from .test_drive_search_excerpts import PassageWorld, ref, texts


@pytest.fixture
def w() -> PassageWorld:
    return PassageWorld()


def clauses(count):
    return [f"Clause {n}. The tenant keeps the garage gate closed after 23:00." for n in range(1, count + 1)]


async def page(w, item, first, *, last=None):
    req = ExcerptRangeRequest(ref(item), first, last or first + 9, page=True)
    return await w.service.excerpt_range("u", req)


def current(w, name, texts, **kw):
    return w.doc(name, texts, [0.1] * len(texts), kind="md", content_version="c1", indexed_version="c1", **kw)


@pytest.fixture
def doc(w):
    return current(w, "Oak Street parking rules.md", clauses(25))


async def test_first_page(w, doc):
    r = await page(w, doc, 1)
    p = r.page
    assert (p.first, p.last, p.total, p.next, p.end, p.past_end) == (1, 10, 25, 11, False, False)
    assert p.version == 1 and p.path == "Oak Street parking rules.md" and p.ref == ref(doc)
    assert len(texts(r)) == 10 and r.continue_at is None


async def test_the_last_page_ends(w, doc):
    r = await page(w, doc, 21)
    p = r.page
    assert (p.first, p.last, p.next, p.end) == (21, 25, None, True)
    assert texts(r) == clauses(25)[20:]


async def test_past_the_end_reads_nothing(w, doc):
    reads = w.repo.calls["texts"]
    r = await page(w, doc, 26)
    p = r.page
    assert p.past_end and p.total == 25 and p.first == 26 and not r.files
    assert w.repo.calls["texts"] == reads


async def test_a_budget_cut_continues_where_it_stopped(w):
    long = "word " * 1_800
    item = w.doc("long.md", [long] * 5, [0.1] * 5, kind="md")
    assert len(long) * 3 > EXCERPTS_CHARS_MAX
    r = await page(w, item, 1)
    assert r.continue_at == 3 and r.page.next == 3 and r.page.last == 2 and not r.page.end


async def test_a_missing_chunk_does_not_move_the_reader_past_it(w, doc):
    del w.repo.chunks[("u", doc, 1, 9)]
    r = await page(w, doc, 1)
    assert len(texts(r)) == 9 and r.page.next == 11 and r.page.last == 9


async def test_a_dropped_tail_chunk_still_ends_the_file(w, doc):
    del w.repo.chunks[("u", doc, 1, 24)]
    r = await page(w, doc, 21)
    assert r.page.end and r.page.next is None


async def test_truncated_and_stale_come_from_the_record(w):
    item = w.doc("big.md", clauses(3), [0.1] * 3, kind="md", truncated=True, content_version="v2",
                 indexed_version="v1")
    p = (await page(w, item, 1)).page
    assert p.truncated and p.stale and p.end


async def test_a_plain_range_has_no_page(w, doc):
    r = await w.service.excerpt_range("u", ExcerptRangeRequest(ref(doc), 6, 8))
    assert r.page is None and len(texts(r)) == 3 and r.continue_at is None


async def test_a_plain_range_cut_keeps_continue_at(w):
    long = "word " * 1_800
    item = w.doc("long.md", [long] * 5, [0.1] * 5, kind="md")
    r = await w.service.excerpt_range("u", ExcerptRangeRequest(ref(item), 1, 5))
    assert r.page is None and r.continue_at == 3
