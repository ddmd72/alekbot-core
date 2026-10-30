"""The Cabinet status carries the readable chunks and this month's model dollars (USER_DRIVE_INDEXING_RFC §6,
§25.4.5; the §24.7 page and image counters are retired, review rv_sv2_p5 MINOR 3): chunks of readable generations only,
and dollars read from the current calendar month's usage document (a past month's document never counts)."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_enrichment import USD_CAPS
from src.domain.drive_index import DriveChunk
from src.domain.drive_index_planner import month_key

from tests.unit.services.test_drive_search_service import World


async def _settled_world() -> World:
    w = World()
    w.drive.add("a.pdf")
    await w.settle()
    return w


def _chunks(item_id, generation, n):
    return [DriveChunk(user_id="u", connection_id="u", item_id=item_id, generation=generation, index=i, text="t", char_start=0,
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
    now = w.clock.now
    await w.repo.add_model_usage("u", month_key(now), now.date().isoformat(), "reader", 1.25, 0.0, 4)
    await w.repo.add_model_usage("u", month_key(now), now.date().isoformat(), "triage", 0.5, 0.0, 2)
    status = await w.search_svc.indexing_status("u")
    assert (status.usd_month["reader"], status.usd_month["triage"]) == (1.25, 0.5)
    assert status.usd_caps == dict(USD_CAPS)


async def test_last_months_usage_reads_as_zero():
    w = await _settled_world()
    last = w.clock.now - timedelta(days=40)
    await w.repo.add_model_usage("u", month_key(last), last.date().isoformat(), "reader", 1.25, 0.0, 4)
    status = await w.search_svc.indexing_status("u")
    assert status.usd_month == {"reader": 0.0, "triage": 0.0, "profile": 0.0, "passage": 0.0, "transcribe": 0.0}


async def test_no_sync_state_has_nothing_to_show():
    w = World()
    status = await w.search_svc.indexing_status("u")
    assert (status.readable_chunks, dict(status.usd_month), status.initial_build_spent_usd) == (0, {}, 0.0)
