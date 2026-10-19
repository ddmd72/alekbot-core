"""Memory telemetry records and pure rules (docs/04_solution_strategy/decisions/memory_telemetry.md).

Pure data and arithmetic: no I/O, no logging. The probe (ports/memory_probe.py) produces a
``ProbeReading``; the service turns readings and finished work units into flat dicts that land in
Cloud Logging as ``jsonPayload`` and from there in BigQuery.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

MIB = 1024 * 1024

BASE_SAMPLE_INTERVAL_S = 15.0
PRESSURE_SAMPLE_INTERVAL_S = 1.0
PRESSURE_SAMPLE_AT_PCT = 75.0
PRESSURE_DUMP_AT_PCT = 85.0

EVENT_BOOT = "memory.boot"
EVENT_SAMPLE = "memory.sample"
EVENT_SCOPE = "memory.scope"
EVENT_PRESSURE = "memory.pressure"
EVENT_PAYLOAD = "memory.payload"

# Work-unit kinds. A fixed set keeps the BigQuery column values groupable.
SCOPE_WORKER_TASK = "worker_task"
SCOPE_CHAT_TURN = "chat_turn"
SCOPE_AGENT_EXECUTION = "agent_execution"
SCOPE_SUBPROCESS = "subprocess"


@dataclass(frozen=True)
class ContainerMemory:
    """What the container runtime meters — the number Cloud Run kills on (includes /tmp)."""
    usage_bytes: int
    limit_bytes: Optional[int] = None
    cache_bytes: Optional[int] = None


@dataclass(frozen=True)
class ChildProcess:
    pid: int
    name: str
    rss_bytes: int
    age_s: float


@dataclass(frozen=True)
class ProbeReading:
    """One look at this process, its children and its container."""
    rss_bytes: int
    container: Optional[ContainerMemory] = None
    peak_rss_bytes: Optional[int] = None
    tmp_bytes: Optional[int] = None
    children: Tuple[ChildProcess, ...] = ()
    threads: Optional[int] = None
    fds: Optional[int] = None


@dataclass(frozen=True)
class ActiveScope:
    """A work unit that is running right now."""
    kind: str
    name: str
    trace_id: Optional[str]
    age_s: float


def to_mib(n_bytes: Optional[int]) -> Optional[float]:
    if n_bytes is None:
        return None
    return round(n_bytes / MIB, 1)


def usage_pct(container: Optional[ContainerMemory]) -> Optional[float]:
    """Container usage as a percentage of its limit; None when either is unknown or unlimited."""
    if container is None or not container.limit_bytes:
        return None
    return round(100.0 * container.usage_bytes / container.limit_bytes, 1)


def sample_interval_s(pct: Optional[float], base_s: float = BASE_SAMPLE_INTERVAL_S) -> float:
    """Sample fast under pressure so the last seconds before a kill are on record."""
    if pct is not None and pct >= PRESSURE_SAMPLE_AT_PCT:
        return min(base_s, PRESSURE_SAMPLE_INTERVAL_S)
    return base_s


def is_pressure_dump_due(pct: Optional[float]) -> bool:
    return pct is not None and pct >= PRESSURE_DUMP_AT_PCT


def _put(fields: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        fields[key] = value


def sample_fields(
    reading: ProbeReading,
    scopes: List[ActiveScope],
    contributors: Mapping[str, float],
    max_children: int = 8,
    max_scopes: int = 12,
) -> Dict[str, Any]:
    """Flat ``jsonPayload`` for one ``memory.sample`` record.

    Children and scopes are capped (largest children first, oldest scopes first) so a runaway
    fan-out cannot turn one sample into a megabyte of log.
    """
    fields: Dict[str, Any] = {"event": EVENT_SAMPLE, "rss_mb": to_mib(reading.rss_bytes)}
    c = reading.container
    if c is not None:
        fields["usage_mb"] = to_mib(c.usage_bytes)
        _put(fields, "limit_mb", to_mib(c.limit_bytes))
        _put(fields, "cache_mb", to_mib(c.cache_bytes))
        _put(fields, "usage_pct", usage_pct(c))
    _put(fields, "rss_peak_mb", to_mib(reading.peak_rss_bytes))
    _put(fields, "tmp_mb", to_mib(reading.tmp_bytes))
    _put(fields, "threads", reading.threads)
    _put(fields, "fds", reading.fds)

    children = sorted(reading.children, key=lambda ch: ch.rss_bytes, reverse=True)
    fields["n_children"] = len(children)
    fields["children_rss_mb"] = to_mib(sum(ch.rss_bytes for ch in children))
    fields["children"] = [
        {"name": ch.name, "pid": ch.pid, "rss_mb": to_mib(ch.rss_bytes), "age_s": round(ch.age_s, 1)}
        for ch in children[:max_children]
    ]

    ordered = sorted(scopes, key=lambda s: s.age_s, reverse=True)
    fields["n_scopes"] = len(ordered)
    fields["scopes"] = [
        {"kind": s.kind, "name": s.name, "trace_id": s.trace_id, "age_s": round(s.age_s, 1)}
        for s in ordered[:max_scopes]
    ]
    if contributors:
        fields["contrib"] = {name: round(value, 1) for name, value in sorted(contributors.items())}
    return fields


@dataclass
class ScopeRecord:
    """Accounting for one finished work unit."""
    kind: str
    name: str
    trace_id: Optional[str]
    duration_s: float
    usage_start_bytes: Optional[int]
    usage_end_bytes: Optional[int]
    usage_peak_bytes: Optional[int]
    concurrent_peak: int
    outcome: str = "ok"
    user_id: Optional[str] = None


def scope_fields(rec: ScopeRecord) -> Dict[str, Any]:
    """Flat ``jsonPayload`` for one ``memory.scope`` record.

    ``delta_mb`` is end minus start of the *container*, so it includes whatever ran concurrently;
    ``concurrent`` says how many scopes overlapped at the busiest moment (1 = alone, the delta is
    this unit's own).
    """
    fields: Dict[str, Any] = {
        "event": EVENT_SCOPE,
        "kind": rec.kind,
        "name": rec.name,
        "dur_s": round(rec.duration_s, 2),
        "concurrent": rec.concurrent_peak,
        "outcome": rec.outcome,
    }
    _put(fields, "trace_id", rec.trace_id)
    _put(fields, "user_id", rec.user_id)
    _put(fields, "usage_start_mb", to_mib(rec.usage_start_bytes))
    _put(fields, "usage_end_mb", to_mib(rec.usage_end_bytes))
    _put(fields, "usage_peak_mb", to_mib(rec.usage_peak_bytes))
    if rec.usage_start_bytes is not None and rec.usage_end_bytes is not None:
        fields["delta_mb"] = to_mib(rec.usage_end_bytes - rec.usage_start_bytes)
    return fields


def payload_fields(provider: str, model: str, n_items: int, n_file_parts: int, file_chars: int) -> Dict[str, Any]:
    """Flat ``jsonPayload`` for one ``memory.payload`` record: how much attachment data a single LLM
    request carries (base64 text, so characters == bytes on the wire). Joined to ``memory.scope`` by
    ``trace_id`` (the log labels carry it), it shows what a long turn re-sends on every round."""
    return {
        "event": EVENT_PAYLOAD,
        "provider": provider,
        "model": model,
        "n_items": n_items,
        "n_file_parts": n_file_parts,
        "file_mb": to_mib(file_chars),
    }


EVENT_CENSUS = "memory.census"
CENSUS_PHASE_TURN_END = "turn_end"
CENSUS_PHASE_IDLE = "idle"
CENSUS_PHASE_RELEASED = "released"
CENSUS_MIN_TURN_S = 30.0       # only long turns are worth a census (it is slow by design)
CENSUS_IDLE_DELAY_S = 60.0     # how long after the turn the idle census runs
CENSUS_COOLDOWN_S = 120.0      # at most one census cycle per this interval


@dataclass(frozen=True)
class CensusSite:
    """One allocation site and the memory it still holds."""
    site: str          # "path/to/file.py:123"
    size_bytes: int
    count: int
    stack: Tuple[str, ...] = ()   # innermost-last frames, only for the heaviest sites


@dataclass(frozen=True)
class CensusReport:
    traced_bytes: int
    peak_bytes: int
    sites: Tuple[CensusSite, ...]


@dataclass(frozen=True)
class ReleaseResult:
    """What an explicit release pass achieved (gc.collect + returning free heap to the OS)."""
    gc_collected: int
    trimmed: bool


def is_census_due(kind: str, duration_s: float, since_last_s: Optional[float]) -> bool:
    """A census follows a long chat turn, at most once per cooldown."""
    if kind != SCOPE_CHAT_TURN or duration_s < CENSUS_MIN_TURN_S:
        return False
    return since_last_s is None or since_last_s >= CENSUS_COOLDOWN_S


def census_fields(
    phase: str,
    report: CensusReport,
    usage_before_bytes: Optional[int],
    usage_after_bytes: Optional[int] = None,
    release: Optional[ReleaseResult] = None,
    top_n: int = 15,
) -> Dict[str, Any]:
    """Flat ``jsonPayload`` for one ``memory.census`` record.

    ``usage_after_mb`` / ``gc_collected`` / ``trimmed`` appear only for a pass that tried to release
    memory: if usage drops after it, the process was holding free pages; if it does not, something
    still references the memory, and ``sites`` says which line allocated it.
    """
    ordered = sorted(report.sites, key=lambda s: s.size_bytes, reverse=True)[:top_n]
    fields: Dict[str, Any] = {
        "event": EVENT_CENSUS,
        "phase": phase,
        "traced_mb": to_mib(report.traced_bytes),
        "traced_peak_mb": to_mib(report.peak_bytes),
        "sites": [
            {"site": s.site, "mb": to_mib(s.size_bytes), "n": s.count, **({"stack": list(s.stack)} if s.stack else {})}
            for s in ordered
        ],
    }
    _put(fields, "usage_before_mb", to_mib(usage_before_bytes))
    _put(fields, "usage_after_mb", to_mib(usage_after_bytes))
    if release is not None:
        fields["gc_collected"] = release.gc_collected
        fields["trimmed"] = release.trimmed
    return fields
