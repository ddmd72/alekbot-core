"""The dry-run kit's counters keep the run guard the way the production store does (F8)."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.consolidation.s3_dry_run_kit import RecordingCounters

T0 = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
SPAN = timedelta(minutes=5)


async def test_a_held_guard_refuses_a_second_run_until_it_expires_or_is_released():
    counters = RecordingCounters()
    assert await counters.acquire_run("o1", T0, T0 + SPAN) is True
    assert await counters.acquire_run("o2", T0 + timedelta(seconds=1), T0 + SPAN) is False
    assert await counters.acquire_run("o2", T0 + SPAN, T0 + 2 * SPAN) is True

    await counters.release_run("o1")           # not the holder: leaves the guard
    assert await counters.acquire_run("o3", T0 + SPAN, T0 + 2 * SPAN) is False
    await counters.release_run("o2")
    assert await counters.acquire_run("o3", T0 + SPAN, T0 + 2 * SPAN) is True


async def test_the_kit_refuses_what_the_production_store_refuses():
    counters = RecordingCounters()
    with pytest.raises(ValueError):
        await counters.acquire_run("", T0, T0 + SPAN)
    with pytest.raises(ValueError):
        await counters.acquire_run("o1", T0, T0)
    with pytest.raises(ValueError):
        await counters.acquire_run("o1", T0.replace(tzinfo=None), T0 + SPAN)
