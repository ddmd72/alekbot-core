"""Search and the access note for files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ55, Δ56).

The search card keeps its tier and says the file exists but cannot be opened; the coverage line counts such files from
the tick's counter. `access_note` answers list and open when the provider says "not found" for an item the index
knows: the same sentence as the search card, by ref or by folder path; an item the index does not know gets None."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_search import INACCESSIBLE_COUNTER, render_results
from src.domain.drive_search_texts import ACCESS_LINE_FILE, ACCESS_NOTE_FILE, ACCESS_NOTE_FOLDER

from .drive_harness_connections import harness_connections
from .test_drive_search_service import World

SINCE = datetime(2026, 10, 11, 12, 30, tzinfo=timezone.utc)


async def _conn(w):
    """The harness connection the World's search reads (access_note takes the connection, MULTI_DRIVE_RFC §6)."""
    return await harness_connections(w.users, w.drive).get("u", "u")


@pytest.fixture
async def w():
    world = World()
    world.folder = world.drive.add("Shared", folder=True)
    world.doc = world.drive.add("citizen card.pdf", parent=world.folder)
    world.open_doc = world.drive.add("lease.pdf")
    await world.settle()
    world.set_state(world.doc, inaccessible_since=SINCE, inaccessible_checked_at=SINCE)
    world.set_state(world.folder, inaccessible_since=SINCE, inaccessible_checked_at=SINCE)
    return world


async def test_the_card_says_the_file_cannot_be_opened_and_coverage_counts_it(w):
    state = w.repo.states["u"]
    w.repo.states["u"] = replace(state, counters={**state.counters, INACCESSIBLE_COUNTER: 1})
    result = await w.search("citizen card")
    text = render_results(result)
    assert ACCESS_LINE_FILE in text
    assert "cannot be opened by the bot" not in text.replace(ACCESS_LINE_FILE, "")     # Δ93: results found
    empty = await w.search("citizen card", kind="docx")     # nothing found: the sentence returns
    assert "1 file cannot be opened by the bot" in render_results(empty)
    assert not empty.cards and empty.envelope.inaccessible == 1


async def test_access_note_by_ref(w):
    assert await w.search_svc.access_note(await _conn(w), item_id=w.doc) == ACCESS_NOTE_FILE
    assert await w.search_svc.access_note(await _conn(w), item_id=w.folder) == ACCESS_NOTE_FOLDER
    assert await w.search_svc.access_note(await _conn(w), item_id=w.open_doc) is None
    assert await w.search_svc.access_note(await _conn(w), item_id="unknown") is None


async def test_access_note_by_folder_path(w):
    assert await w.search_svc.access_note(await _conn(w), path="Shared") == ACCESS_NOTE_FOLDER
    assert await w.search_svc.access_note(await _conn(w), path="/shared/") == ACCESS_NOTE_FOLDER   # as the search scope matches
    assert await w.search_svc.access_note(await _conn(w), path="Nowhere") is None
