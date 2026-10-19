"""Review fixes to files the bot cannot open in the tick (USER_DRIVE_INDEXING_RFC §26.2, review of S1, 2026-10-14).

- A tick interrupted right after a marking never saves its counters; a settled tick always counts the marked files,
  so the notice's gate cannot be lost for good.
- The tick reads the marked records once (lean), not twice in full, unless it changed a marking itself.
- The tick-start read sits inside the tick's error handling: an index that is still building ends the tick as an
  error, never as an exception out of the tick.
- A folder the bot cannot open marks its sub-folders too (they inherit its permissions), so listing one by path gives
  the access sentence; a folder that can be opened again makes its marked sub-folders due for a re-read."""
import pytest

from src.domain.drive_index import DriveIndexStoreUnavailable, inaccessible
from src.domain.drive_index_params import INACCESSIBLE_RETRY_MIN, INACCESSIBLE_RETRY_SLOW
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER
from src.domain.user_drive import DriveThrottled

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_inaccessible_world import ClosedWorld

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ClosedWorld()


def counters(w):
    return w.repo.states["u"].counters


async def test_a_tick_interrupted_after_a_marking_still_gets_counted(w):
    folder = w.drive.add("Shared", folder=True)
    doc = w.add_doc("scan.md", prose(40), parent=folder)
    w.drive.closed.add(doc)
    real = w.drive.get_change

    async def throttled_parent(user_id, item_id):
        if item_id == folder:
            raise DriveThrottled(60)   # the parent check after the marking ends the tick: no counters saved
        return await real(user_id, item_id)
    w.drive.get_change = throttled_parent
    await w.settle()
    assert inaccessible(w.rec(doc))
    assert counters(w).get(INACCESSIBLE_COUNTER) == 1 and counters(w).get(INACCESSIBLE_UNNOTIFIED_COUNTER) == 1


async def test_the_marked_records_are_read_once_per_quiet_tick_and_lean(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    calls = []
    real = w.repo.inaccessible_records

    async def spy(user_id, limit, **kw):
        calls.append(kw)
        return await real(user_id, limit, **kw)
    w.repo.inaccessible_records = spy
    w.clock.advance(60)
    await w.tick()   # nothing marked or cleared in this tick
    assert calls == [{"lean": True}]
    assert counters(w).get(INACCESSIBLE_COUNTER) == 1


async def test_a_store_error_at_the_tick_start_ends_the_tick_as_an_error(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()

    async def building(user_id, limit, **kw):
        raise DriveIndexStoreUnavailable("inaccessible_records: FailedPrecondition")
    w.repo.inaccessible_records = building
    w.clock.advance(60)
    result = await w.tick()
    assert result.status == "error"


async def test_a_closed_folder_marks_its_sub_folders(w):
    outer = w.drive.add("Shared", folder=True)
    inner = w.drive.add("Scans", parent=outer, folder=True)
    deeper = w.drive.add("2026", parent=inner, folder=True)
    doc = w.add_doc("scan.md", prose(40), parent=outer)
    w.drive.closed.update({outer, inner, deeper, doc})
    await w.settle()
    assert inaccessible(w.rec(outer)) and inaccessible(w.rec(inner)) and inaccessible(w.rec(deeper))


async def test_a_folder_opened_again_makes_its_marked_sub_folders_due(w):
    outer = w.drive.add("Shared", folder=True)
    inner = w.drive.add("Scans", parent=outer, folder=True)
    doc = w.add_doc("scan.md", prose(40), parent=outer)
    w.drive.closed.update({outer, inner, doc})
    await w.settle()
    w.drive.closed.clear()
    w.clock.advance(INACCESSIBLE_RETRY_SLOW.total_seconds())
    await w.settle()   # the outer folder and the file are read again on the timer
    assert not inaccessible(w.rec(outer))
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds() + 60)
    await w.settle()
    assert not inaccessible(w.rec(inner))
    assert w.rec(doc) is not None and not inaccessible(w.rec(doc))
