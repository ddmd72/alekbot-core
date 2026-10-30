"""Files the bot cannot open, in the tick (USER_DRIVE_INDEXING_RFC §26.2, D33, Δ47–Δ52, Δ54, Δ56).

Existence is decided by delta alone: a 404 on an id-addressed call made while indexing a known item marks it
inaccessible, never deletes it; only a tombstone removes it. A marked record is re-read on an observation of it or of its
parent folder at most once per INACCESSIBLE_RETRY_MIN, and otherwise every INACCESSIBLE_RETRY_SLOW; a readable answer
clears the marking silently and the file is read as usual."""
from datetime import timedelta

import pytest

from src.domain.drive_index import Nudge, RecordState, TriageVerdict, inaccessible
from src.domain.drive_index_params import INACCESSIBLE_RETRY_MIN, INACCESSIBLE_RETRY_SLOW
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER, NOT_READ_YET_COUNTER

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_inaccessible_world import ClosedWorld
from .drive_index_fakes import U_SCOPE

pytestmark = pytest.mark.usefixtures("admitted")


@pytest.fixture
def w():
    return ClosedWorld()


def counters(w):
    return w.repo.states["u"].counters


async def test_a_download_404_marks_the_file_and_never_deletes_it(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    rec = w.rec(doc)
    assert rec is not None and inaccessible(rec)
    assert rec.state is RecordState.PENDING and rec.reason is None
    assert (rec.attempts, rec.attempt_started_at) == (0, None)   # no attempt counted, never `failed`
    assert w.parser.requests == [] and w.summary_total("inaccessible") == 1
    assert rec.has_card   # the metadata card stays (§26.2)


async def test_a_tombstone_still_deletes_a_marked_file(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    w.drive.delete(doc)
    await w.settle()
    assert w.rec(doc) is None


async def test_readable_again_on_the_slow_retry_clears_silently_and_is_read(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    marked_calls = w.drive.calls_for(doc)
    w.drive.closed.discard(doc)
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds() + 60)
    await w.settle()
    assert inaccessible(w.rec(doc)) and w.drive.calls_for(doc) == marked_calls   # no observation: no early retry
    w.clock.advance(INACCESSIBLE_RETRY_SLOW.total_seconds())
    await w.settle()
    rec = w.rec(doc)
    assert not inaccessible(rec) and rec.state is RecordState.INDEXED and w.readable(doc)
    assert rec.inaccessible_checked_at is None and not rec.inaccessible_reobserved


async def test_an_observation_of_the_item_retries_at_most_once_per_retry_min(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    base = w.drive.calls_for(doc)
    w.clock.advance(300)
    w.drive.reemit(doc)
    await w.settle()
    assert w.rec(doc).inaccessible_reobserved and w.drive.calls_for(doc) == base   # due, but not before the cap
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds())
    await w.settle()
    assert w.drive.calls_for(doc) == base + 1 and inaccessible(w.rec(doc))   # one read, still closed
    w.drive.reemit(doc)
    await w.settle()
    w.drive.reemit(doc)
    await w.settle()
    assert w.drive.calls_for(doc) == base + 1   # re-emitted twice within the hour: no read
    w.drive.closed.discard(doc)
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds())
    await w.settle()
    assert not inaccessible(w.rec(doc)) and w.rec(doc).state is RecordState.INDEXED


async def test_an_observation_of_the_parent_folder_makes_a_marked_file_due(w):
    folder = w.drive.add("Shared", folder=True)
    doc = w.add_doc("scan.md", prose(40), parent=folder)
    w.drive.closed.add(doc)
    await w.settle()
    assert inaccessible(w.rec(doc)) and not inaccessible(w.rec(folder))   # the folder answered: not marked
    w.drive.closed.discard(doc)
    w.clock.advance(300)
    w.drive.reemit(folder)
    await w.settle()
    assert w.rec(doc).inaccessible_reobserved
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds())
    await w.settle()
    assert not inaccessible(w.rec(doc)) and w.readable(doc)


async def test_a_folder_is_marked_through_its_file_and_the_next_marking_climbs_a_level(w):
    outer = w.drive.add("Outer", folder=True)
    inner = w.drive.add("Inner", parent=outer, folder=True)
    first = w.add_doc("a.md", prose(40), parent=inner)
    w.drive.closed.update({outer, inner, first})
    await w.settle()
    assert inaccessible(w.rec(first)) and inaccessible(w.rec(inner))
    assert not inaccessible(w.rec(outer))   # one level per marking
    assert w.rec(inner).has_card and w.rec(inner).state is RecordState.METADATA_ONLY
    second = w.add_doc("b.md", prose(40), parent=inner)
    w.drive.closed.add(second)
    await w.settle()
    assert inaccessible(w.rec(second)) and inaccessible(w.rec(outer))


async def test_the_parent_lookup_orphan_rule_is_unchanged(w):
    doc = w.add_doc("lost.md", prose(5), parent="ghost")   # its parent was never listed by delta
    await w.settle()
    assert w.rec(doc) is None and "ghost" in w.drive.get_change_calls


async def test_a_nudge_404_writes_nothing(w):
    doc = w.add_doc("a.md", prose(40))
    await w.settle()
    before = w.rec(doc)
    assert before.state is RecordState.INDEXED
    w.drive.closed.add(doc)
    await w.tick(Nudge(mutation_id="m1", item_ids=(doc,)))
    after = w.rec(doc)
    assert after is not None and not inaccessible(after) and after.state is RecordState.INDEXED


async def test_a_triage_thumbnail_404_marks_the_image(w):
    img = w.add_image("IMG_1.jpg")
    other = w.add_image("IMG_2.jpg")
    w.drive.closed.add(img)
    await w.settle()
    assert inaccessible(w.rec(img)) and w.rec(img).triage_verdict is None
    assert w.rec(other).triage_verdict is TriageVerdict.PHOTO
    assert [i.item_id for batch in w.triage.calls for i in batch] == [other]


async def test_a_readable_generation_is_kept_while_marked(w):
    doc = w.add_doc("a.md", prose(40))
    await w.settle()
    readable = w.readable(doc)
    assert readable
    w.drive.edit(doc, prose(45).encode(), modified_at=w.clock.now - timedelta(hours=1))
    w.drive.closed.add(doc)
    await w.settle()
    rec = w.rec(doc)
    assert inaccessible(rec) and rec.stale and w.readable(doc) == readable   # Δ52: still served, shown stale


async def test_the_tick_never_writes_the_notified_flag(w):
    doc = w.add_doc("scan.md", prose(40))
    w.drive.closed.add(doc)
    await w.settle()
    since = w.rec(doc).inaccessible_since
    await w.repo.set_inaccessible_notified(U_SCOPE, {doc: since})
    w.clock.advance(INACCESSIBLE_RETRY_SLOW.total_seconds())
    await w.settle()   # a failed re-check rewrites the record
    rec = w.rec(doc)
    assert rec.inaccessible_checked_at >= since + INACCESSIBLE_RETRY_SLOW and rec.inaccessible_notified_since == since


async def test_counters_count_inaccessible_files_apart_from_not_read_yet(w):
    doc = w.add_doc("scan.md", prose(40))
    waiting = w.add_doc("later.md", prose(40))
    w.drive.closed.add(doc)
    w.drive.download_errors[waiting] = OSError("reset")   # one transient failure: still waiting to be read
    await w.tick()
    await w.tick()
    c = counters(w)
    assert c.get(INACCESSIBLE_COUNTER) == 1 and c.get(INACCESSIBLE_UNNOTIFIED_COUNTER) == 1
    assert c.get(NOT_READ_YET_COUNTER, 0) == (0 if w.readable(waiting) else 1)   # the closed file is not "waiting"
    await w.repo.set_inaccessible_notified(U_SCOPE, {doc: w.rec(doc).inaccessible_since})
    await w.settle()
    assert counters(w).get(INACCESSIBLE_UNNOTIFIED_COUNTER, 0) == 0 and counters(w).get(INACCESSIBLE_COUNTER) == 1
