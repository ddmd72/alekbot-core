"""`DriveOperations.open` and the open bound (USER_DRIVE_INDEXING_RFC §29): the refusal is decided on the extent before
any download, the cut after the conversion, a failed lookup fails safe, vision opens are untouched. The conversion
fake records downloads; the search fake answers `text_extent` per item."""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.drive_index_params import OPEN_MAX_CHARS
from src.domain.drive_op import DriveOp
from src.domain.drive_open_bound import refusal_text
from src.domain.drive_open_cut import cut_line
from src.domain.drive_open_bound_text import BoundText
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import REFERENCE_REMINDER
from src.domain.drive_text_extent import ExtentState, ExtentWhy, TextExtent
from src.domain import drive_open_texts as T
from src.domain.user_drive import DriveConnection, DriveItem
from src.infrastructure.drive_operations import DriveOperations
from src.services.user_drive_service import UserDriveService

from .test_drive_operations_uncovered_paths import CTX, FakeConversion

PATH = "Docs/Oak Street parking rules.md"
RULES = DriveItem(item_id="r1", name="Oak Street parking rules.md", path=PATH, is_folder=False, size_bytes=300_000,
                  mime_type="text/markdown")
SCAN = DriveItem(item_id="p1", name="Garage plan.pdf", path="Docs/Garage plan.pdf", is_folder=False, size_bytes=5000,
                 mime_type="application/pdf")
PHOTO = DriveItem(item_id="i1", name="Space 14.jpg", path="Docs/Space 14.jpg", is_folder=False, size_bytes=5000,
                  mime_type="image/jpeg")
CALL = DriveItem(item_id="c1", name="Rome hotel booking RM-55120.m4a", path="Calls/Rome hotel booking RM-55120.m4a",
                 is_folder=False, size_bytes=5000, mime_type="audio/mp4")


def long_text(chars: int) -> str:
    paragraphs, size, n = [], 0, 0
    while size < chars:
        n += 1
        paragraphs.append(f"Clause {n}. The tenant keeps the garage gate closed after 23:00.")
        size += len(paragraphs[-1]) + 2
    return "\n\n".join(paragraphs)


def pageable(chars, *, passages=130, truncated=False):
    return TextExtent(ExtentState.PAGEABLE, text_chars=chars, passages=passages, truncated=truncated, version=1,
                      pages_total=passages)


class FakeExtentSearch:
    """`text_extent` answers per item id (or raises); `transcript_text` and `archived_text` are scripted."""

    def __init__(self, extents=None, *, raises=None, transcripts=None) -> None:
        self.extents = extents or {}
        self.raises = raises
        self.transcripts = transcripts or {}
        self.extent_asks: list[str] = []
        self.transcript_asks: list[str] = []

    async def text_extent(self, user_id, item_id):
        self.extent_asks.append(item_id)
        if self.raises is not None:
            raise self.raises
        return self.extents.get(item_id, TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.NOT_READ))

    async def transcript_text(self, user_id, item_id, *, path="", label=""):
        self.transcript_asks.append(item_id)
        return self.transcripts.get(item_id)

    async def archived_text(self, *a, **kw):
        return None


def _ops(conversion, search, *, note=None):
    drive = AsyncMock(spec=UserDriveService)
    drive.connection.return_value = DriveConnection(connected=True, drive_id="d1")
    kw = {}
    if note is not None:
        kw.update(drive_notes=note)
    return DriveOperations(conversion_service=conversion, drive_service=drive, drive_search=search, **kw)


def _conv(item, text="", data=b"%PDF"):
    return FakeConversion({f"drive:{item.item_id}": item}, data=data, text=text)


async def _open(ops, item):
    return await ops.open(CTX, "u1", f"drive:{item.item_id}")


# -- the refusal, before any download ------------------------------------------------------------------------------

async def test_a_pageable_text_over_the_cap_is_refused_and_nothing_is_downloaded():
    conv = _conv(RULES, long_text(200_000))
    extent = pageable(200_000)
    out = await _open(_ops(conv, FakeExtentSearch({"r1": extent})), RULES)
    assert (out.op, out.cls, out.success, out.body) == (DriveOp.OPEN, OutcomeClass.REFUSED, False, False)
    assert out.text == refusal_text(PATH, extent)
    assert out.facts.too_long and out.items == (RULES,) and out.open_bound.extent == extent
    assert conv.text_reads == [] and conv.byte_reads == []


async def test_a_truncated_file_is_refused_whatever_its_indexed_length():
    conv = _conv(RULES, "short")
    extent = pageable(1_000, truncated=True)
    out = await _open(_ops(conv, FakeExtentSearch({"r1": extent})), RULES)
    assert out.cls is OutcomeClass.REFUSED and out.text == refusal_text(PATH, extent)
    assert conv.text_reads == []


async def test_a_pageable_text_at_the_cap_opens_whole():
    text = long_text(OPEN_MAX_CHARS)[:OPEN_MAX_CHARS]
    out = await _open(_ops(_conv(RULES, text), FakeExtentSearch({"r1": pageable(len(text))})), RULES)
    assert out.cls is OutcomeClass.OK and text in out.text and not out.facts.cut and out.open_bound is None
    assert "[Cut by the system" not in out.text


async def test_a_pageable_text_that_is_longer_live_is_cut_with_the_indexed_line():
    live = long_text(OPEN_MAX_CHARS + 30_000)
    extent = pageable(40_000)
    out = await _open(_ops(_conv(RULES, live), FakeExtentSearch({"r1": extent})), RULES)
    assert out.cls is OutcomeClass.OK and out.body and out.facts.cut
    kept = out.open_bound.kept
    assert kept <= OPEN_MAX_CHARS and out.open_bound.chars == len(live)
    assert cut_line(f'"{PATH}"', BoundText("", len(live), kept, ExtentState.PAGEABLE), extent) in out.text


async def test_a_stale_text_over_the_cap_is_cut_with_the_stale_line():
    live = long_text(OPEN_MAX_CHARS + 20_000)
    extent = TextExtent(ExtentState.STALE, text_chars=999_999, passages=77, version=1)
    out = await _open(_ops(_conv(RULES, live), FakeExtentSearch({"r1": extent})), RULES)
    assert out.facts.cut and "marked stale" in out.text and "77 passages" in out.text


@pytest.mark.parametrize("why", [w for w in ExtentWhy])
async def test_a_not_indexed_text_over_the_cap_is_cut_with_its_reason(why):
    live = long_text(OPEN_MAX_CHARS + 20_000)
    extent = TextExtent(ExtentState.NOT_INDEXED, why)
    out = await _open(_ops(_conv(RULES, live), FakeExtentSearch({"r1": extent})), RULES)
    assert out.facts.cut and T.WHY[why] in out.text


async def test_a_short_not_indexed_text_opens_whole_without_a_line():
    out = await _open(_ops(_conv(RULES, "tiny"), FakeExtentSearch()), RULES)
    assert out.cls is OutcomeClass.OK and "tiny" in out.text and not out.facts.cut


# -- vision and recordings -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("item", [SCAN, PHOTO])
async def test_vision_opens_ignore_a_huge_pageable_extent(item, caplog):
    conv = _conv(item)
    with caplog.at_level(logging.INFO):
        out = await _open(_ops(conv, FakeExtentSearch({item.item_id: pageable(5_000_000, passages=4000)})), item)
    assert out.cls is OutcomeClass.OK and out.file_data is not None
    assert conv.byte_reads == [f"drive:{item.item_id}"]
    lines = [r.getMessage() for r in caplog.records if "opened vision" in r.getMessage()]
    assert lines == [f"[DriveOperations] opened vision: drive:{item.item_id} mime={item.mime_type} bytes=4 pages=4000"]


async def test_a_recording_with_an_over_cap_pageable_extent_is_refused_before_its_transcript():
    search = FakeExtentSearch({"c1": pageable(300_000)}, transcripts={"c1": "TRANSCRIPT"})
    conv = _conv(CALL)
    out = await _open(_ops(conv, search), CALL)
    assert out.cls is OutcomeClass.REFUSED and out.facts.too_long
    assert search.transcript_asks == [] and conv.byte_reads == [] and conv.text_reads == []


async def test_a_recording_within_the_cap_opens_as_its_transcript():
    search = FakeExtentSearch({"c1": pageable(10_000)}, transcripts={"c1": "TRANSCRIPT"})
    out = await _open(_ops(_conv(CALL), search), CALL)
    assert out.cls is OutcomeClass.OK and "TRANSCRIPT" in out.text and not out.facts.cut


async def test_a_live_transcript_that_cannot_be_paged_and_is_over_the_cap_is_marked_cut():
    extent = TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.NO_TEXT, text_chars=0)
    stale = TextExtent(ExtentState.STALE, text_chars=OPEN_MAX_CHARS + 5, passages=3)
    out = await _open(_ops(_conv(CALL), FakeExtentSearch({"c1": stale}, transcripts={"c1": "T"})), CALL)
    assert out.facts.cut
    out = await _open(_ops(_conv(CALL), FakeExtentSearch({"c1": extent}, transcripts={"c1": "T"})), CALL)
    assert not out.facts.cut


async def test_the_recordings_link_never_asks_for_the_extent():
    search = FakeExtentSearch({"c1": pageable(300_000)})
    out = await _ops(_conv(CALL), search).open(CTX, "u1", "drive:c1", original=True)
    assert out.cls is OutcomeClass.OK and out.body and search.extent_asks == []


# -- a failed lookup fails safe (L7) -------------------------------------------------------------------------------

async def test_a_raising_lookup_opens_and_still_cuts(caplog):
    live = long_text(70_000)
    with caplog.at_level(logging.WARNING):
        out = await _open(_ops(_conv(RULES, live), FakeExtentSearch(raises=RuntimeError("index down"))), RULES)
    assert out.facts.cut and out.open_bound.kept <= OPEN_MAX_CHARS and T.WHY[ExtentWhy.UNKNOWN] in out.text
    warnings = [r.getMessage() for r in caplog.records if "extent lookup failed" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_lookup_returning_a_mock_counts_as_unknown(caplog):
    search = MagicMock()
    search.text_extent = AsyncMock(return_value=MagicMock())
    search.archived_text = AsyncMock(return_value=None)
    with caplog.at_level(logging.WARNING):
        out = await _open(_ops(_conv(RULES, long_text(70_000)), search), RULES)
    assert out.facts.cut
    assert len([r for r in caplog.records if "extent lookup failed" in r.getMessage()]) == 1


async def test_a_search_without_text_extent_is_unknown(caplog):
    class Bare:
        async def archived_text(self, *a, **kw):
            return None

    with caplog.at_level(logging.WARNING):
        out = await _open(_ops(_conv(RULES, long_text(70_000)), Bare()), RULES)
    assert out.facts.cut and out.cls is OutcomeClass.OK


# -- the note block, the wrapper and the logs ----------------------------------------------------------------------

class _Notes:
    async def drive_id_for_connection(self, user_id, connection):
        return "d1"

    async def note_for_item(self, user_id, drive_id, item_id):
        from types import SimpleNamespace
        return SimpleNamespace(text="the garage rules", doc_type=None, life_areas=())


async def test_the_cut_line_is_outside_the_wrapper_and_before_the_note_block():
    live = long_text(OPEN_MAX_CHARS + 5_000)
    extent = TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.OFF)
    out = await _open(_ops(_conv(RULES, live), FakeExtentSearch({"r1": extent}), note=_Notes()), RULES)
    text = out.text
    first_wrapper_end = text.index("</drive_document")
    line = text.index("[Cut by the system")
    assert text.startswith(REFERENCE_REMINDER) and line > first_wrapper_end
    assert text.index("the garage rules") > line and out.facts.owner_note


async def test_the_log_lines(caplog):
    live = long_text(OPEN_MAX_CHARS + 5_000)
    with caplog.at_level(logging.INFO):
        await _open(_ops(_conv(RULES, live), FakeExtentSearch({"r1": TextExtent(ExtentState.NOT_INDEXED, ExtentWhy.OFF)})), RULES)
        await _open(_ops(_conv(RULES, "x"), FakeExtentSearch({"r1": pageable(250_000, passages=140, truncated=True)})), RULES)
    messages = [r.getMessage() for r in caplog.records]
    assert f"[DriveOperations] opened: drive:r1 chars={len(live)}" in messages
    assert any(m.startswith("[DriveOperations] open cut: drive:r1 chars=") and m.endswith("extent=not_indexed")
               for m in messages)
    assert "[DriveOperations] open refused: drive:r1 chars=250000 passages=140 truncated=True" in messages
