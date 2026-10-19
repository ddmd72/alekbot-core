"""MemoryTelemetryService: sampling, scope accounting, failure isolation."""
import asyncio

import pytest

from src.domain.memory_telemetry import MIB, ContainerMemory, ProbeReading
from src.ports.memory_probe import MemoryProbePort
from src.services.memory_telemetry_service import MemoryTelemetryService
from src.utils import memory_scope


class FakeProbe(MemoryProbePort):
    def __init__(self, usages, limit=1024 * MIB, fail=False):
        self.usages = list(usages)
        self.limit = limit
        self.fail = fail
        self.last = usages[0]

    def _next(self):
        if self.fail:
            raise OSError("boom")
        if self.usages:
            self.last = self.usages.pop(0)
        return self.last

    def read(self):
        u = self._next()
        return ProbeReading(rss_bytes=u // 2, container=ContainerMemory(usage_bytes=u, limit_bytes=self.limit))

    def usage_bytes(self):
        return self._next()

    def describe(self):
        return {"cgroup": "v1", "container_readable": True}


def _service(probe):
    records = []
    svc = MemoryTelemetryService(probe, "alek-bot-dev", "rev-1", emit=lambda m, f: records.append((m, f)))
    return svc, records


@pytest.fixture(autouse=True)
def _clean_hooks():
    yield
    memory_scope.uninstall_hooks()


@pytest.mark.asyncio
async def test_start_emits_boot_then_samples_and_stop_removes_hooks():
    svc, records = _service(FakeProbe([100 * MIB] * 50))
    svc._base_interval_s = 0.01
    await svc.start()
    await asyncio.sleep(0.05)
    await svc.stop()

    events = [f["event"] for _, f in records]
    assert events[0] == "memory.boot" and "memory.sample" in events
    boot = records[0][1]
    assert boot["probe_cgroup"] == "v1" and boot["svc"] == "alek-bot-dev" and boot["rev"] == "rev-1"
    assert memory_scope._hooks is None
    n = len(records)
    await asyncio.sleep(0.03)
    assert len(records) == n  # loop is really stopped


@pytest.mark.asyncio
async def test_sample_carries_identity_contributors_and_task_count():
    svc, records = _service(FakeProbe([300 * MIB]))
    svc.register_contributor("attach_mb", lambda: 12.34)
    svc.sample_once()

    f = records[0][1]
    assert f["event"] == "memory.sample" and f["usage_mb"] == 300.0 and f["usage_pct"] == 29.3
    assert f["contrib"] == {"attach_mb": 12.3}
    assert f["boot"] == svc._boot and f["tasks"] >= 1


def test_scope_records_start_end_peak_and_concurrency():
    # reads: scope A start=100, scope B start=150, sample=400, B end=200, A end=180
    svc, records = _service(FakeProbe([100 * MIB, 150 * MIB, 400 * MIB, 200 * MIB, 180 * MIB]))
    memory_scope.install_hooks(svc)

    with memory_scope.memory_scope("chat_turn", "slack", trace_id="tr_a"):
        with memory_scope.memory_scope("agent_execution", "smart", trace_id="tr_a"):
            svc.sample_once()
    by_kind = {f["kind"]: f for _, f in records if f["event"] == "memory.scope"}

    a, b = by_kind["chat_turn"], by_kind["agent_execution"]
    assert (a["usage_start_mb"], a["usage_end_mb"], a["usage_peak_mb"], a["delta_mb"]) == (100.0, 180.0, 400.0, 80.0)
    assert (b["usage_start_mb"], b["usage_end_mb"], b["usage_peak_mb"]) == (150.0, 200.0, 400.0)
    assert a["concurrent"] == 2 and b["concurrent"] == 2
    assert a["trace_id"] == "tr_a" and a["outcome"] == "ok"
    sample = next(f for _, f in records if f["event"] == "memory.sample")
    assert sample["n_scopes"] == 2


def test_scope_outcome_error_and_cancel():
    svc, records = _service(FakeProbe([1 * MIB] * 10))
    memory_scope.install_hooks(svc)

    with pytest.raises(ValueError):
        with memory_scope.memory_scope("worker_task", "x"):
            raise ValueError("bad")
    with pytest.raises(asyncio.CancelledError):
        with memory_scope.memory_scope("worker_task", "y"):
            raise asyncio.CancelledError()

    outcomes = [f["outcome"] for _, f in records if f["event"] == "memory.scope"]
    assert outcomes == ["error", "cancelled"]
    assert memory_scope.active_scopes() == []  # nothing leaked


def test_scope_is_a_noop_without_hooks():
    with memory_scope.memory_scope("chat_turn", "slack"):
        assert memory_scope.active_scopes() == []


def test_failing_probe_never_raises_and_warns_once(caplog):
    svc, records = _service(FakeProbe([1 * MIB], fail=True))
    memory_scope.install_hooks(svc)

    assert svc.sample_once() is None
    assert svc.sample_once() is None
    with memory_scope.memory_scope("chat_turn", "slack"):
        pass

    scope = [f for _, f in records if f["event"] == "memory.scope"]
    assert len(scope) == 1 and "usage_start_mb" not in scope[0]  # work unit still recorded, usage unknown
    assert sum("MemoryTelemetry: read failed" in r.getMessage() for r in caplog.records) == 1


def test_failing_contributor_is_dropped_but_others_survive():
    svc, records = _service(FakeProbe([10 * MIB]))
    svc.register_contributor("bad", lambda: 1 / 0)
    svc.register_contributor("good", lambda: 5.0)
    svc.sample_once()

    assert records[0][1]["contrib"] == {"good": 5.0}


def test_emit_failure_is_swallowed():
    svc = MemoryTelemetryService(FakeProbe([1 * MIB]), "s", "r", emit=lambda m, f: 1 / 0)
    assert svc.sample_once() is not None


# -- census ----------------------------------------------------------------

from src.domain.memory_telemetry import CensusReport, CensusSite, ReleaseResult  # noqa: E402
from src.ports.memory_census import MemoryCensusPort  # noqa: E402


class FakeCensus(MemoryCensusPort):
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def start(self): self.calls.append("start")
    def stop(self): self.calls.append("stop")

    def snapshot(self, top_n):
        self.calls.append("snapshot")
        if self.fail:
            raise RuntimeError("no tracing")
        return CensusReport(10 * MIB, 12 * MIB, (CensusSite("a.py:1", 8 * MIB, 4),))

    def release(self):
        self.calls.append("release")
        return ReleaseResult(gc_collected=3, trimmed=True)


@pytest.mark.asyncio
async def test_long_chat_turn_triggers_census_cycle_with_release():
    census = FakeCensus()
    records = []
    svc = MemoryTelemetryService(FakeProbe([500 * MIB] * 3 + [300 * MIB] * 20), "s", "r",
                                 emit=lambda m, f: records.append(f), census=census, census_idle_delay_s=0.01)
    svc._census_last = None
    memory_scope.install_hooks(svc)
    svc.on_scope_end(ScopeRecordLike(kind="chat_turn", duration_s=120.0))
    await svc._census_task

    phases = [r["phase"] for r in records if r["event"] == "memory.census"]
    assert phases == ["turn_end", "idle", "released"]
    assert census.calls == ["snapshot", "snapshot", "release", "snapshot"]
    released = [r for r in records if r.get("phase") == "released"][0]
    assert released["gc_collected"] == 3 and released["trimmed"] is True
    assert released["usage_before_mb"] > released["usage_after_mb"]
    assert released["sites"][0]["site"] == "a.py:1"


def ScopeRecordLike(kind, duration_s):
    from src.domain.memory_telemetry import ScopeRecord
    return ScopeRecord(kind, "chat", "tr", duration_s, None, None, None, 1)


@pytest.mark.asyncio
async def test_short_turn_and_other_kinds_do_not_trigger_census():
    census = FakeCensus()
    svc = MemoryTelemetryService(FakeProbe([1 * MIB] * 5), "s", "r", emit=lambda m, f: None, census=census)
    svc.on_scope_end(ScopeRecordLike("chat_turn", 5.0))
    svc.on_scope_end(ScopeRecordLike("agent_execution", 500.0))
    assert svc._census_task is None and census.calls == []


@pytest.mark.asyncio
async def test_cooldown_blocks_a_second_cycle():
    census = FakeCensus()
    svc = MemoryTelemetryService(FakeProbe([1 * MIB] * 50), "s", "r", emit=lambda m, f: None,
                                 census=census, census_idle_delay_s=0.01)
    svc.on_scope_end(ScopeRecordLike("chat_turn", 100.0))
    first = svc._census_task
    await first
    svc.on_scope_end(ScopeRecordLike("chat_turn", 100.0))
    assert svc._census_task is first  # cooldown: no new task


@pytest.mark.asyncio
async def test_census_failure_is_contained():
    svc = MemoryTelemetryService(FakeProbe([1 * MIB] * 10), "s", "r", emit=lambda m, f: None,
                                 census=FakeCensus(fail=True), census_idle_delay_s=0.01)
    svc.on_scope_end(ScopeRecordLike("chat_turn", 100.0))
    await svc._census_task  # must not raise


@pytest.mark.asyncio
async def test_stop_cancels_a_running_census():
    svc = MemoryTelemetryService(FakeProbe([1 * MIB] * 10), "s", "r", emit=lambda m, f: None,
                                 census=FakeCensus(), census_idle_delay_s=30.0)
    svc.on_scope_end(ScopeRecordLike("chat_turn", 100.0))
    await asyncio.sleep(0.05)
    await svc.stop()
    assert svc._census_task is None
