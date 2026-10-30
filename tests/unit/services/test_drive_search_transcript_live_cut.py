"""A live transcript's cut line names the record's real state (USER_DRIVE_INDEXING_RFC §29.10): the ordinary open's line
for pageable, stale and each not-indexed reason. The real service over the in-memory index; the extent is the real one."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.domain import drive_open_texts as T
from src.domain.drive_index import Scope
from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_text_extent import ExtentWhy
from src.domain.drive_transcript import Segment, StitchedTranscript, StoredTranscript
from src.domain.user_drive import DriveConnection

from .test_drive_search_excerpts import PassageWorld

CHUNKS = [f"Clause {n}. The tenant keeps the garage gate closed after 23:00." for n in range(1, 41)]


def _long():
    segs = tuple(Segment(float(i * 10), None, f"Clause {i}. The tenant keeps the garage gate closed. " * 8)
                 for i in range(400))
    return StoredTranscript("k1", StitchedTranscript(segments=segs, language_codes=("en",), speakers=1,
                                                     duration_s=4000.0))


async def _world(*, chunks=CHUNKS, parent=None, **fields):
    w = PassageWorld()
    kw = dict(kind="m4a", content_version="c1", indexed_version="c1", transcript_state="current",
              transcript_key="k1")
    kw.update(fields)
    if parent is not None:
        kw["parent"] = parent
    item = w.doc("Rome hotel booking RM-55120.m4a", chunks, [0.1] * len(chunks), **kw) if chunks \
        else w.file("Rome hotel booking RM-55120.m4a", kind="m4a", content_version="c1", transcript_state="current",
                    transcript_key="k1")
    await w.repo.put_transcript("u", _long())
    return w, item


async def _text(w, item):
    return await w.service.transcript_text(await w.connection(), item)


def _line(text):
    return text.rpartition("[Cut by the system: ")[2]


async def test_pageable_gets_the_indexed_line():
    w, item = await _world()
    text = await _text(w, item)
    assert "The rest can be read through the index" in text and len(text) < OPEN_MAX_CHARS + 2_000
    assert "[Transcript cut at" not in text


async def test_stale_gets_the_stale_line_naming_the_old_version():
    w, item = await _world(content_version="c2")
    text = await _text(w, item)
    assert "marked stale" in text and f"({len(CHUNKS)} passages" in text
    assert T.WHY[ExtentWhy.NO_TEXT] not in text


async def test_indexing_off_names_that_reason():
    w, item = await _world()
    w.users.enabled["u"] = False
    text = await _text(w, item)
    assert T.WHY[ExtentWhy.OFF] in _line(text)


async def test_a_disconnected_drive_names_that_reason():
    w, item = await _world()
    w.drive.connection_state = DriveConnection(connected=False)
    assert T.WHY[ExtentWhy.NOT_CONNECTED] in _line(await _text(w, item))


async def test_a_rebuilding_index_names_that_reason():
    w, item = await _world()
    w.repo.states["u"] = replace(w.repo.states["u"], purging=True)
    assert T.WHY[ExtentWhy.REBUILDING] in _line(await _text(w, item))


async def test_a_record_with_no_indexed_text_says_so():
    w, item = await _world(chunks=[])
    assert T.WHY[ExtentWhy.NO_TEXT] in _line(await _text(w, item))


async def test_a_file_under_an_opaque_folder_names_that_reason():
    w = PassageWorld()
    code = w.folder("Code")
    w.set(code, scope=Scope.OPAQUE_ROOT)
    item = w.doc("call.m4a", CHUNKS, [0.1] * len(CHUNKS), parent=code, kind="m4a", content_version="c1",
                 indexed_version="c1", transcript_state="current", transcript_key="k1")
    await w.repo.put_transcript("u", _long())
    assert T.WHY[ExtentWhy.HIDDEN] in _line(await _text(w, item))


async def test_the_pointer_names_the_mirror_path():
    w, item = await _world()
    w.service._active_mirror_path = _async("_Transcripts/Oak Street call.md")
    assert "The full text is in _Transcripts/Oak Street call.md." in await _text(w, item)


async def test_a_failed_lookup_says_the_index_could_not_be_asked(monkeypatch):
    w, item = await _world()

    async def boom(*a, **k):
        raise RuntimeError("index down")
    monkeypatch.setattr(w.service, "text_extent", boom)
    assert T.WHY[ExtentWhy.UNKNOWN] in _line(await _text(w, item))


def _async(value):
    async def f(*a, **k):
        return value
    return f
