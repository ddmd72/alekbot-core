"""Pure rules of the memory telemetry records (decisions/memory_telemetry.md)."""
from src.domain.memory_telemetry import (
    MIB, ActiveScope, ChildProcess, ContainerMemory, ProbeReading, ScopeRecord,
    is_pressure_dump_due, sample_fields, sample_interval_s, scope_fields, to_mib, usage_pct,
)


def test_usage_pct_needs_a_limit():
    assert usage_pct(None) is None
    assert usage_pct(ContainerMemory(usage_bytes=500 * MIB)) is None
    assert usage_pct(ContainerMemory(usage_bytes=500 * MIB, limit_bytes=0)) is None
    assert usage_pct(ContainerMemory(usage_bytes=512 * MIB, limit_bytes=1024 * MIB)) == 50.0


def test_sample_interval_tightens_under_pressure():
    assert sample_interval_s(None) == 15.0
    assert sample_interval_s(74.9) == 15.0
    assert sample_interval_s(75.0) == 1.0
    assert sample_interval_s(99.0, base_s=30.0) == 1.0
    assert sample_interval_s(80.0, base_s=0.5) == 0.5  # never slower than the base


def test_pressure_dump_threshold():
    assert not is_pressure_dump_due(None)
    assert not is_pressure_dump_due(84.9)
    assert is_pressure_dump_due(85.0)


def test_sample_fields_flatten_and_cap():
    children = tuple(ChildProcess(pid=i, name=f"c{i}", rss_bytes=i * MIB, age_s=1.0) for i in range(1, 12))
    reading = ProbeReading(
        rss_bytes=300 * MIB, peak_rss_bytes=320 * MIB, tmp_bytes=40 * MIB, threads=9, fds=33,
        container=ContainerMemory(usage_bytes=400 * MIB, limit_bytes=1024 * MIB, cache_bytes=60 * MIB),
        children=children,
    )
    scopes = [ActiveScope("chat_turn", "slack", "tr_1", 5.0), ActiveScope("worker_task", "x", None, 50.0)]
    f = sample_fields(reading, scopes, {"attach_mb": 17.04}, max_children=3, max_scopes=1)

    assert f["event"] == "memory.sample"
    assert (f["rss_mb"], f["usage_mb"], f["limit_mb"], f["cache_mb"], f["usage_pct"]) == (300.0, 400.0, 1024.0, 60.0, 39.1)
    assert (f["tmp_mb"], f["threads"], f["fds"], f["rss_peak_mb"]) == (40.0, 9, 33, 320.0)
    assert f["n_children"] == 11 and len(f["children"]) == 3
    assert [c["name"] for c in f["children"]] == ["c11", "c10", "c9"]  # biggest first
    assert f["children_rss_mb"] == float(sum(range(1, 12)))
    assert f["n_scopes"] == 2 and [s["name"] for s in f["scopes"]] == ["x"]  # oldest first
    assert f["contrib"] == {"attach_mb": 17.0}


def test_sample_fields_omit_what_is_unknown():
    f = sample_fields(ProbeReading(rss_bytes=10 * MIB), [], {})
    assert "usage_mb" not in f and "limit_mb" not in f and "tmp_mb" not in f
    assert "contrib" not in f
    assert f["n_children"] == 0 and f["children"] == [] and f["scopes"] == []


def test_scope_fields_delta_and_optional_parts():
    rec = ScopeRecord("chat_turn", "slack", "tr_9", 12.345, 300 * MIB, 380 * MIB, 410 * MIB, 3, user_id="u1")
    f = scope_fields(rec)
    assert f == {
        "event": "memory.scope", "kind": "chat_turn", "name": "slack", "dur_s": 12.35, "concurrent": 3,
        "outcome": "ok", "trace_id": "tr_9", "user_id": "u1", "usage_start_mb": 300.0,
        "usage_end_mb": 380.0, "usage_peak_mb": 410.0, "delta_mb": 80.0,
    }
    bare = scope_fields(ScopeRecord("subprocess", "node", None, 0.5, None, None, None, 1, outcome="error"))
    assert "delta_mb" not in bare and "trace_id" not in bare and bare["outcome"] == "error"


def test_to_mib_none_passthrough():
    assert to_mib(None) is None
    assert to_mib(1536 * 1024) == 1.5


def test_payload_fields():
    from src.domain.memory_telemetry import payload_fields
    assert payload_fields("openai", "gpt-6-luna", 62, 5, 23 * MIB) == {
        "event": "memory.payload", "provider": "openai", "model": "gpt-6-luna",
        "n_items": 62, "n_file_parts": 5, "file_mb": 23.0,
    }
