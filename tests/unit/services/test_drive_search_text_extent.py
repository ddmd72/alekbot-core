"""`DriveSearchService.text_extent`: what the index holds of one file's text (USER_DRIVE_INDEXING_RFC §29.4 L5, L3)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain.drive_index import Scope
from src.domain.drive_text_extent import ExtentState, ExtentWhy
from src.domain.user_drive import DriveConnection

from .test_drive_search_excerpts import PassageWorld


@pytest.fixture
def w() -> PassageWorld:
    return PassageWorld()


def doc(w, name, texts, distances, **kw):
    """A current record (the fixture's default is stale: no indexed_version)."""
    kw.setdefault("content_version", "c1")
    kw.setdefault("indexed_version", kw["content_version"])
    return w.doc(name, texts, distances, **kw)


async def extent(w, item):
    return await w.service.text_extent(await w.connection(), item)


async def test_pageable_carries_the_numbers(w):
    item = doc(w, "Oak Street parking rules.md", ["Clause 1.", "Clause 2.", "Clause 3."], [0.1] * 3, kind="md")
    e = await extent(w, item)
    rec = w.repo.records[("u", item)]
    assert (e.state, e.why) == (ExtentState.PAGEABLE, None)
    assert (e.text_chars, e.passages, e.pages_total, e.version, e.truncated) == (rec.text_chars, 3, 3, 1, False)


async def test_truncated_is_flagged(w):
    item = doc(w, "Oak Street parking rules.md", ["Clause 1."], [0.1], kind="md", truncated=True)
    e = await extent(w, item)
    assert e.state is ExtentState.PAGEABLE and e.truncated


async def test_stale_keeps_the_old_passages(w):
    item = doc(w, "Oak Street parking rules.md", ["Clause 1.", "Clause 2."], [0.1, 0.1], kind="md",
                 content_version="v2", indexed_version="v1")
    e = await extent(w, item)
    assert (e.state, e.passages, e.version) == (ExtentState.STALE, 2, 1)


async def test_indexing_off(w):
    item = doc(w, "a.md", ["x"], [0.1], kind="md")
    w.users.enabled["u"] = False
    e = await extent(w, item)
    assert (e.state, e.why) == (ExtentState.NOT_INDEXED, ExtentWhy.OFF)


async def test_disconnected_and_another_drive(w):
    item = doc(w, "a.md", ["x"], [0.1], kind="md")
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
    assert (await extent(w, item)).why is ExtentWhy.NOT_CONNECTED
    w.drive.connection_state = DriveConnection(connected=False)
    assert (await extent(w, item)).why is ExtentWhy.NOT_CONNECTED


async def test_rebuilding_on_purge(w):
    item = doc(w, "a.md", ["x"], [0.1], kind="md")
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True)
    assert (await extent(w, item)).why is ExtentWhy.REBUILDING


async def test_awaiting_a_rebuild():
    w = PassageWorld(migrated=False)
    item = doc(w, "a.md", ["x"], [0.1], kind="md")
    assert (await extent(w, item)).why is ExtentWhy.REBUILDING


async def test_no_record_is_not_read(w):
    assert (await extent(w, "missing")).why is ExtentWhy.NOT_READ


async def test_a_metadata_only_record_has_no_text(w):
    item = w.file("budget.ods", kind="ods")
    assert (await extent(w, item)).why is ExtentWhy.NO_TEXT


async def test_a_folder_has_no_text(w):
    folder = w.folder("Docs")
    assert (await extent(w, folder)).why is ExtentWhy.NO_TEXT


async def test_a_file_under_an_opaque_ancestor_is_hidden(w):
    code = w.folder("Code")
    w.set(code, scope=Scope.OPAQUE_ROOT)
    item = doc(w, "notes.md", ["x"], [0.1], parent=code, kind="md")
    assert (await extent(w, item)).why is ExtentWhy.HIDDEN


async def test_the_lookup_reads_one_record_and_no_chunk(w):
    item = doc(w, "a.md", ["x", "y"], [0.1, 0.1], kind="md")
    before = dict(w.repo.calls)
    await extent(w, item)
    assert dict(w.repo.calls) == before
    assert w.repo.knn_limits == [] and w.emb.calls == []


async def test_pageable_means_page_one_returns_text():
    """L3: whatever the state of the world, a file whose extent is pageable has a first page."""
    from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
    from .test_drive_search_excerpts import ref
    cases = []

    def build(setup):
        w = PassageWorld()
        item = setup(w)
        cases.append((w, item))

    build(lambda w: doc(w, "a.md", ["x"], [0.1], kind="md"))
    build(lambda w: doc(w, "a.md", ["x"], [0.1], kind="md", truncated=True))
    build(lambda w: doc(w, "a.md", ["x"], [0.1], kind="md", content_version="v2", indexed_version="v1"))
    build(lambda w: w.file("b.ods", kind="ods"))
    build(lambda w: w.folder("Docs"))
    build(lambda w: doc(w, "a.md", ["x"], [0.1], parent=_opaque(w), kind="md"))
    assert cases
    for w, item in cases:
        e = await w.service.text_extent(await w.connection(), item)
        page = await w.service.excerpt_range("u", ExcerptRangeRequest(ref(item), 1, 1, page=True))
        returns_text = bool(page.files)
        if e.state is ExtentState.PAGEABLE:
            assert returns_text
        if not returns_text:
            assert e.state is ExtentState.NOT_INDEXED


def _opaque(w):
    code = w.folder("Code")
    w.set(code, scope=Scope.OPAQUE_ROOT)
    return code
