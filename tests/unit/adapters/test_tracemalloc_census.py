"""TracemallocCensus on a real allocation (stdlib only)."""
import tracemalloc

import pytest

from src.adapters.tracemalloc_census import TracemallocCensus, _short


@pytest.fixture
def census():
    c = TracemallocCensus(frames=6)
    c.start()
    yield c
    c.stop()


def _hold_five_megabytes():
    return [bytearray(1024 * 1024) for _ in range(5)]


def test_snapshot_attributes_held_memory_to_its_allocation_site(census):
    held = _hold_five_megabytes()  # noqa: F841 - kept alive on purpose
    report = census.snapshot(top_n=10)

    assert report.traced_bytes >= 5 * 1024 * 1024
    sites = [s for s in report.sites if "test_tracemalloc_census.py" in s.site]
    assert sites, [s.site for s in report.sites]
    assert sites[0].size_bytes >= 5 * 1024 * 1024 and sites[0].count >= 5


def test_snapshot_drops_memory_after_it_is_released(census):
    held = _hold_five_megabytes()
    before = census.snapshot(top_n=5).traced_bytes
    del held
    census.release()
    after = census.snapshot(top_n=5).traced_bytes
    assert after < before - 4 * 1024 * 1024


def test_heaviest_site_carries_a_stack(census):
    held = _hold_five_megabytes()  # noqa: F841
    top = census.snapshot(top_n=3).sites[0]
    assert top.stack and all(":" in f for f in top.stack)


def test_snapshot_when_not_tracing_is_empty_not_an_error():
    c = TracemallocCensus()
    c.stop()
    assert not tracemalloc.is_tracing()
    report = c.snapshot(top_n=5)
    assert report.sites == () and report.traced_bytes == 0


def test_release_reports_what_it_did():
    result = TracemallocCensus().release()
    assert result.gc_collected >= 0 and isinstance(result.trimmed, bool)


def test_start_and_stop_are_idempotent():
    c = TracemallocCensus()
    c.start(); c.start()
    assert tracemalloc.is_tracing()
    c.stop(); c.stop()
    assert not tracemalloc.is_tracing()


def test_short_path_keeps_module_identity():
    assert _short("/app/venv/lib/python3.13/site-packages/logfire/_internal/x.py") == "logfire/_internal/x.py"
    assert _short("/app/src/services/foo.py") == "src/services/foo.py"
