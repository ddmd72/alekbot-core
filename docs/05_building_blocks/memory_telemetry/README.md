<!-- living-doc
covers:
  src/domain/memory_telemetry.py
  src/services/memory_telemetry_service.py
  src/composition/memory_telemetry_setup.py
  src/ports/memory_census.py
  src/ports/memory_probe.py
  src/adapters/proc_memory_probe.py
-->
# Memory Telemetry

## Purpose

Cloud Run kills a container when its metered memory crosses the limit, and a kill leaves no log of what was running.
This block puts that figure on record: every process samples its own memory, brackets units of work, and writes
structured log lines that a log sink routes to BigQuery. It answers "how much memory did the container use, and which
work units were open when it peaked". It is a measurement tool only: it never gates, throttles or restarts anything,
and it never raises into the code it measures (a failed probe or contributor is logged once and skipped,
`MemoryTelemetryService._warn_once`).

It does not attribute memory to a unit exactly (see Invariants), and it is not the place for cost or latency: those are
`prompt_content` and Logfire (root `CLAUDE.md` § Debugging Cloud Run). Rationale:
[`memory_telemetry.md`](../../04_solution_strategy/decisions/memory_telemetry.md).

## Owned data

No Firestore. The block owns a family of records, each a flat `jsonPayload` with `event`, `svc` (service name),
`rev` (revision) and `boot` (a per-process id), built by pure functions in `src/domain/memory_telemetry.py` and
logged through `MemoryTelemetryService._safe_emit`. The sink and dataset are infrastructure outside the repo
(`docs/07_deployment/README.md` § Memory telemetry sink).

| `event` | Written | Fields that matter |
|---|---|---|
| `memory.boot` | once per process start (`MemoryTelemetryService._emit_boot`) | `interval_s`, `pid`, `python`, `probe_*` (what `MemoryProbePort.describe` found readable; `probe_cgroup` is `v1`, `v2` or `none`) |
| `memory.sample` | every tick (`sample_fields`) | `usage_mb`, `limit_mb`, `usage_pct`, `cache_mb`, `rss_mb`, `rss_peak_mb`, `tmp_mb`, `threads`, `fds`, `tasks`, `n_children`, `children[]`, `n_scopes`, `scopes[]`, `contrib{}` |
| `memory.scope` | when a work unit ends (`scope_fields`) | `kind`, `name`, `trace_id`, `user_id`, `dur_s`, `usage_start_mb`, `usage_end_mb`, `usage_peak_mb`, `delta_mb`, `concurrent`, `outcome` (`ok`, `error`, `cancelled`) |
| `memory.payload` | an OpenAI request carries file parts (`payload_fields`, emitted by `src/adapters/openai_adapter.py`) | `provider`, `model`, `n_items`, `n_file_parts`, `file_mb` (base64 characters, so bytes on the wire) |
| `memory.census` | opt-in, see Flows (`census_fields`) | `phase`, `traced_mb`, `traced_peak_mb`, `sites[]` (`site`, `mb`, `n`, `stack` for the heaviest), and for `released`: `usage_before_mb`, `usage_after_mb`, `gc_collected`, `trimmed` |

- `usage_mb` is the container's metered memory (cgroup), the figure Cloud Run kills on. It includes the in-memory
  writable layer (`/tmp`, reported as `tmp_mb`), which `rss_mb` does not.
- `children[]` and `scopes[]` are capped (`sample_fields`: largest children first, oldest scopes first), so use
  `n_children` / `n_scopes` for the true count.
- `kind` is one of the `SCOPE_*` constants in `src/domain/memory_telemetry.py` (`worker_task`, `chat_turn`,
  `agent_execution`, `subprocess`); a fixed set keeps the column groupable.
- `contrib{}` holds named numbers registered with `MemoryTelemetryService.register_contributor`. Nothing registers one
  today.
- `EVENT_PRESSURE` and `is_pressure_dump_due` are defined, but nothing emits `memory.pressure`.

## Flows

### Wiring

`src/composition/memory_telemetry_setup.py::build_memory_telemetry` is called once by each entry point (`main.py`,
`indexer_main.py`, `relay_main.py`, `job_main.py`), which then awaits `MemoryTelemetryService.start` and `stop`. The
service name is `K_SERVICE`, else `CLOUD_RUN_JOB`, else the entry point's default. It returns `None` when switched off.
Knobs are optional and read with `os.getenv` at the call site (the repo rule for optional knobs, root `CLAUDE.md`):
`MEMORY_TELEMETRY` (`off`/`0`/`false`/`no` disables), `MEMORY_SAMPLE_INTERVAL_S` (floor 1 s), `MEMORY_TRACEMALLOC`,
`MEMORY_TRACEMALLOC_FRAMES`.

### Sampling

1. `MemoryTelemetryService.start` emits `memory.boot`, installs the scope hooks, and starts the sampler task.
2. `MemoryTelemetryService.sample_once` reads `ProcMemoryProbe.read` (cgroup usage, `/proc` for this process and its
   children, `statvfs` of `/tmp`), raises the peak of every open scope (`memory_scope.note_usage`), and emits one
   `memory.sample`.
3. The next sleep comes from `sample_interval_s`: the base interval (`BASE_SAMPLE_INTERVAL_S`), or one second once
   `usage_pct` reaches `PRESSURE_SAMPLE_AT_PCT`, so the last seconds before a kill are on record.

### Work units

`src/utils/memory_scope.py::memory_scope` is a context manager that call sites wrap around a unit of work. It is a
no-op until the service installs its hooks, so call sites need no guard and tests and `MEMORY_TELEMETRY=off` pay
nothing. Call sites: the worker route (`src/web/main_service_routes.py`, `worker_task`), the conversation handler
(`src/handlers/conversation_handler.py`, `chat_turn`), `BaseAgent` execution (`agent_execution`), and the drive-parser
and sandbox child launchers (`subprocess`). On entry a scope reads container usage; on exit it emits `memory.scope`
through `MemoryTelemetryService.on_scope_end`.

### Census (opt-in diagnostic)

With `MEMORY_TRACEMALLOC` set, `build_memory_telemetry` starts a `TracemallocCensus` (`MemoryCensusPort`) at process
start, so only allocations made afterwards are attributed. After a `chat_turn` scope of at least `CENSUS_MIN_TURN_S`,
at most once per `CENSUS_COOLDOWN_S` (`is_census_due`), `MemoryTelemetryService._census_cycle` emits three records:

- `turn_end`: which lines still hold memory the moment the turn finishes.
- `idle`: the same after `CENSUS_IDLE_DELAY_S` with nothing running. Sites that stay are referenced, not free.
- `released`: after `MemoryCensusPort.release` (`gc.collect()` then `malloc_trim(0)`). If `usage_after_mb` falls well
  below `usage_before_mb`, the process held free allocator pages. If it does not fall and `traced_mb` stays high, a
  live reference holds the memory and `sites[0]` names the allocating line.

## Invariants and traps

- **`delta_mb` is the container's change, not the unit's.** It includes everything that ran alongside. It is the
  unit's own cost only when `concurrent = 1`; otherwise read the overlapping `memory.sample` rows. Overlap is counted
  by `src/utils/memory_scope.py::_Open.concurrent_peak`.
- **`usage_peak_mb` can miss a spike shorter than the sample interval.** It is the highest sample a tick saw while the
  scope was open, plus its start and end reads.
- **`memory.payload` covers OpenAI only.** Claude, Gemini and Grok requests are not measured. It joins to
  `memory.scope` by `trace_id`, which for a payload row is in the log labels, not in `jsonPayload`.
- **The census is slow by design.** `tracemalloc` slows the process and holds bookkeeping memory: switch it on for one
  investigation window and off after. `traced_mb` counts Python allocations since tracing started; `usage_mb` is the
  whole container, so the two differ by the interpreter, native libraries and the tracing bookkeeping.
- **Cloud Run throttles CPU outside requests** (request-based billing). The sampler then runs in bursts (observed
  earlier: 10-24 s gaps at a 5 s interval) and a census, which needs tens of seconds of CPU after the response has
  gone out, never completes. For a census window turn CPU throttling off on the service (instance-based billing while
  it is off) and restore it afterwards. That is a live Cloud Run change: the owner decides it.
- **Rows are not time.** The sampling interval depends on pressure and on `MEMORY_SAMPLE_INTERVAL_S`, so weight rows by
  their interval when computing a duration (third query below). `boot` separates processes of one revision.

## Queries

BigQuery, dataset `alek_memory_dev`, table `run_googleapis_com_stdout`, date-partitioned (retention: the deployment
doc). Replace `<PROJECT_ID>`. The columns are fields of `jsonPayload`; numeric fields may arrive as strings, hence the
`CAST`s.

Peak container memory per hour, per service:

```sql
SELECT TIMESTAMP_TRUNC(timestamp, HOUR) AS hr, jsonPayload.svc AS svc,
       MAX(CAST(jsonPayload.usage_mb AS FLOAT64)) AS peak_mb
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.sample' AND timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
GROUP BY hr, svc ORDER BY hr
```

Which work units were open at the highest samples (the black box for an OOM):

```sql
SELECT timestamp, jsonPayload.svc, jsonPayload.usage_pct, jsonPayload.n_scopes, jsonPayload.scopes
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.sample' AND CAST(jsonPayload.usage_pct AS FLOAT64) >= 85
ORDER BY timestamp DESC LIMIT 50
```

How long memory stayed above a level, per process. A sample stands for its sleep interval: one second at or above
`PRESSURE_SAMPLE_AT_PCT` (75), otherwise the base interval, 15 s unless `MEMORY_SAMPLE_INTERVAL_S` changed it
(`memory.boot` carries `interval_s`):

```sql
SELECT jsonPayload.svc AS svc, jsonPayload.boot AS boot,
       SUM(IF(CAST(jsonPayload.usage_pct AS FLOAT64) >= 75, 1, 15)) / 60 AS minutes_over_500mb
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.sample' AND CAST(jsonPayload.usage_mb AS FLOAT64) > 500
GROUP BY svc, boot ORDER BY minutes_over_500mb DESC
```

Heaviest work units by memory growth, alone only:

```sql
SELECT jsonPayload.kind AS kind, jsonPayload.name AS name, COUNT(*) AS n,
       AVG(CAST(jsonPayload.delta_mb AS FLOAT64)) AS avg_delta_mb, MAX(CAST(jsonPayload.dur_s AS FLOAT64)) AS max_s
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.scope' AND CAST(jsonPayload.concurrent AS INT64) = 1
GROUP BY kind, name ORDER BY avg_delta_mb DESC
```

## Links

- Decision: [`memory_telemetry.md`](../../04_solution_strategy/decisions/memory_telemetry.md).
- Sink and dataset setup, knobs: [`docs/07_deployment/README.md`](../../07_deployment/README.md) § Memory telemetry sink.
- Debug-source table (which source answers which question): root `CLAUDE.md` § Debugging Cloud Run.

## Where to start reading

- `src/domain/memory_telemetry.py::sample_fields` and `scope_fields`: the record shapes and thresholds.
- `src/services/memory_telemetry_service.py::MemoryTelemetryService`: sampler loop, scope hooks, census cycle.
- `src/utils/memory_scope.py::memory_scope`: how a work unit is bracketed and how overlap is counted.
- `src/adapters/proc_memory_probe.py::ProcMemoryProbe`: what is read from cgroup and `/proc`.
- `src/composition/memory_telemetry_setup.py::build_memory_telemetry`: wiring and env knobs.
