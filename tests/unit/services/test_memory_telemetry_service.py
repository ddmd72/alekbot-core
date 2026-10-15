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
