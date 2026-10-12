"""The Cabinet status carries the readable chunks and this month's model usage (USER_DRIVE_INDEXING_RFC §6, §24.7):
chunks of readable generations only, and usage counted in the current calendar month (a past month reads as zero
before the tick resets it)."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index import DriveChunk
from src.domain.drive_index_planner import month_key

from tests.unit.services.test_drive_search_service import World


async def _settled_world() -> World:
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    return w


def _chunks(item_id, generation, n):
    return [DriveChunk(user_id="u", item_id=item_id, generation=generation, index=i, text="t", char_start=0,
                       char_end=1, kind="pdf") for i in range(n)]


async def test_readable_chunks_count_only_indexed_generations():
    w = await _settled_world()
    item_id = next(iter(w.drive.items))
    w.set_state(item_id, indexed_generation=2, chunk_count=3)
    await w.repo.write_chunks(_chunks(item_id, 2, 3) + _chunks(item_id, 3, 2), [[0.0] * 8] * 5)
    status = await w.search_svc.indexing_status("u")
    assert status.readable_chunks == 3


async def test_this_months_model_usage_reaches_the_status():
    w = await _settled_world()
    state = await w.repo.get_sync_state("u")
    await w.repo.save_sync_state(replace(state, usage_month=month_key(w.clock.now), model_pages_used=120,
                                         triage_images_used=48))
    status = await w.search_svc.indexing_status("u")
    assert (status.model_pages_month, status.triage_images_month) == (120, 48)


async def test_last_months_usage_reads_as_zero():
    w = await _settled_world()
    state = await w.repo.get_sync_state("u")
    last_month = month_key(w.clock.now - timedelta(days=40))
    await w.repo.save_sync_state(replace(state, usage_month=last_month, model_pages_used=120, triage_images_used=48))
    status = await w.search_svc.indexing_status("u")
    assert (status.model_pages_month, status.triage_images_month) == (0, 0)


async def test_no_sync_state_has_nothing_to_show():
    w = World()
    status = await w.search_svc.indexing_status("u")
    assert (status.readable_chunks, status.model_pages_month, status.triage_images_month) == (0, 0, 0)
