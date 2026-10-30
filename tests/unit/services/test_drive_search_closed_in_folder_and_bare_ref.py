"""DriveSearchService (slice 1b): a passages request whose file ref holds no drive ref at all gets the stale-ref line;
a folder-scoped search too large to hold in memory counts the files the bot cannot open inside that folder (and not the
ones elsewhere). Technical ids only."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_excerpt_range_request import ExcerptRangeRequest
from src.domain.drive_ref_refusal import stale_ref_line
from src.domain.drive_search import INACCESSIBLE_COUNTER
from src.domain.user_drive import DriveRefRefused
from src.services import drive_search_service as search_mod

from .test_drive_search_service import World
from .test_drive_search_service_connections import _two, world as connections_world   # noqa: F401  (fixture)

SINCE = datetime(2026, 10, 11, 12, 30, tzinfo=timezone.utc)


async def test_a_file_ref_without_any_drive_ref_gets_the_stale_line(connections_world):
    await _two(connections_world)
    with pytest.raises(DriveRefRefused) as refused:
        await connections_world.service.excerpt_range("usr-1", ExcerptRangeRequest("report.pdf", 1, 2))
    assert refused.value.line == stale_ref_line("report.pdf")


async def test_a_wide_folder_search_counts_the_closed_files_inside_the_folder_only(monkeypatch):
    monkeypatch.setattr(search_mod, "FOLDER_INMEM_CARDS", 1)       # the subtree walk is incomplete: no in-memory subtree
    w = World()
    inside = w.drive.add("Shared", folder=True)
    closed_in = w.drive.add("citizen card.pdf", parent=inside)
    w.drive.add("lease.pdf", parent=inside)
    closed_out = w.drive.add("passport.pdf")
    await w.settle()
    for item in (closed_in, closed_out):
        w.set_state(item, inaccessible_since=SINCE, inaccessible_checked_at=SINCE)
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, counters={**state.counters, INACCESSIBLE_COUNTER: 2})
    result = await w.search("citizen card", folder="Shared", kind="docx")     # nothing found: the count is shown
    assert not result.cards
    assert result.tiered.coverage.inaccessible == 1     # the counter says 2; this folder holds 1
