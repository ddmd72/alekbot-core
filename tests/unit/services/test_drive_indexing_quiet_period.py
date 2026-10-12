"""Office Online autosave churn (USER_DRIVE_INDEXING_RFC §7.3, §5.5 class 6, S0b): an edit is indexed once, after
QUIET without saves or after MAX_DEFER of continuous saving; a bot write (a nudge) at once."""
from datetime import timedelta

import pytest

from src.domain.drive_index import Nudge, RecordState
from src.domain.drive_index_params import MAX_DEFER, QUIET

from .drive_content_world import OOXML_HEAD, ContentWorld, admitted, prose  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("admitted")
SAVE_EVERY = timedelta(minutes=2)   # S0b: Word for the web saved every 1.2-3.3 min while typing


@pytest.fixture
async def w():
    world = ContentWorld()
    world.doc = world.add_doc("Договор_ред3.docx", prose(20))
    await world.settle()
    return world


def runs(w):
    return sum(1 for r in w.parser.requests if r.name == "Договор_ред3.docx")


async def autosave(w, minutes):
    """Saves every SAVE_EVERY for `minutes`, a tick after each one (the fan-out and nudges keep ticking)."""
    for n in range(int(timedelta(minutes=minutes) / SAVE_EVERY)):
        w.clock.advance(SAVE_EVERY.total_seconds())
        w.drive.edit(w.doc, OOXML_HEAD + prose(20 + n).encode(), modified_at=w.clock.now)
        await w.settle()


async def test_churn_costs_one_run_after_the_quiet_period(w):
    await autosave(w, 30)
    assert runs(w) == 1   # only the first version so far
    rec = w.rec(w.doc)
    assert rec.state is RecordState.PENDING and rec.indexed_version != rec.content_version and rec.stale
    last_save = w.clock.now
    w.clock.advance((QUIET - timedelta(minutes=1)).total_seconds())
    await w.settle()
    assert runs(w) == 1
    w.clock.now = last_save + QUIET
    await w.settle()
    rec = w.rec(w.doc)
    assert runs(w) == 2 and rec.state is RecordState.INDEXED and rec.indexed_version == rec.content_version
    assert rec.dirty_since is None


async def test_continuous_saving_is_indexed_at_max_defer(w):
    first_dirty = w.clock.now + SAVE_EVERY
    indexed_at = []
    w.parser.before = lambda request: indexed_at.append(w.clock.now)
    await autosave(w, MAX_DEFER.total_seconds() / 60 + 10)
    assert len(indexed_at) == 1 and first_dirty + MAX_DEFER <= indexed_at[0] < first_dirty + MAX_DEFER + SAVE_EVERY


async def test_a_nudge_is_indexed_at_once(w):
    w.drive.edit(w.doc, OOXML_HEAD + prose(30).encode(), modified_at=w.clock.now)
    w.drive.log.pop()   # delta has not caught up yet (S0d)
    await w.tick(Nudge("m1", (w.doc,)))
    rec = w.rec(w.doc)
    assert runs(w) == 2 and rec.indexed_version == rec.content_version
