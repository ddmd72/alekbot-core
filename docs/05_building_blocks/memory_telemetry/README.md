# Memory telemetry — building block

What it records, how to read it, and what it cannot tell you. Decision and rationale:
`docs/04_solution_strategy/decisions/memory_telemetry.md`. Sink setup: `docs/07_deployment/README.md`.

## Records (all are `jsonPayload` with `event`, `svc`, `rev`, `boot`)

| `event` | When | Key fields |
|---|---|---|
| `memory.boot` | process start | `probe_cgroup` (`v1`/`v2`/`none`), which sources are readable, `interval_s` |
| `memory.sample` | every 15 s; every 1 s at ≥ 75% of the limit | `usage_mb`, `limit_mb`, `usage_pct`, `rss_mb`, `rss_peak_mb`, `cache_mb`, `tmp_mb`, `children[]`, `n_scopes`, `scopes[]`, `threads`, `fds`, `tasks`, `contrib{}` |
| `memory.scope` | a work unit ends | `kind` (`worker_task`/`chat_turn`/`agent_execution`/`subprocess`), `name`, `trace_id`, `dur_s`, `usage_start_mb`, `usage_end_mb`, `usage_peak_mb`, `delta_mb`, `concurrent`, `outcome` |
| `memory.payload` | an OpenAI request carries attachments | `model`, `n_file_parts`, `file_mb` (base64 on the wire) |
| `memory.census` | opt-in (`MEMORY_TRACEMALLOC=1`), after a chat turn of ≥ 30 s: at `turn_end`, 60 s later at `idle`, then `released` | `phase`, `traced_mb`, `sites[]` (`site` = file:line, `mb`, `n`, `stack` for the heaviest), and for `released`: `usage_before_mb`, `usage_after_mb`, `gc_collected`, `trimmed` |

`usage_mb` is the container's metered memory (cgroup), the figure Cloud Run kills on. It includes the
in-memory writable layer (`/tmp`), which RSS does not.

## Reading it honestly

- A scope's `delta_mb` is the *container's* change over the scope, so it includes everything that ran
  alongside. Trust it as that scope's own cost only when `concurrent = 1`; otherwise read the overlapping
  `memory.sample` rows and the other scopes.
- `usage_peak_mb` is the highest sample a tick saw while the scope was open (and its start/end reads), so
  it can miss a spike shorter than the sample interval. Under pressure the interval is 1 s.
- `memory.payload` joins to scopes by `trace_id` (log labels carry it). Only the OpenAI adapter emits it
  so far; Claude, Gemini and Grok requests are not yet measured.

## Queries (BigQuery, dataset `alek_memory_dev`, table `run_googleapis_com_stdout`)

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
SELECT timestamp, jsonPayload.usage_pct, jsonPayload.n_scopes, jsonPayload.scopes
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.sample' AND CAST(jsonPayload.usage_pct AS FLOAT64) >= 85
ORDER BY timestamp DESC LIMIT 50
```

How long memory stayed above a level (minutes ≈ samples × interval):

```sql
SELECT jsonPayload.boot AS boot, COUNT(*) * 15 / 60 AS minutes_over_500mb
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.sample' AND CAST(jsonPayload.usage_mb AS FLOAT64) > 500
GROUP BY boot ORDER BY minutes_over_500mb DESC
```

Heaviest work units by memory growth, alone only:

```sql
SELECT jsonPayload.kind AS kind, jsonPayload.name AS name, COUNT(*) AS n,
       AVG(CAST(jsonPayload.delta_mb AS FLOAT64)) AS avg_delta_mb, MAX(CAST(jsonPayload.dur_s AS FLOAT64)) AS max_s
FROM `<PROJECT_ID>.alek_memory_dev.run_googleapis_com_stdout`
WHERE jsonPayload.event = 'memory.scope' AND jsonPayload.concurrent = 1
GROUP BY kind, name ORDER BY avg_delta_mb DESC
```

## Census (opt-in, slow by design)

`MEMORY_TRACEMALLOC=1` (optional `MEMORY_TRACEMALLOC_FRAMES`, default 4) starts `tracemalloc` at process start;
the process then runs noticeably slower and holds bookkeeping memory, so use it for one investigation window and
switch it off after. Reading a cycle:

- `turn_end` — which lines still hold memory the moment the turn finishes.
- `idle` — the same a minute later, with nothing running. Sites that stay are referenced, not free.
- `released` — after `gc.collect()` and `malloc_trim(0)`. If `usage_after_mb` drops well below
  `usage_before_mb`, the process was holding free pages (allocator), not live objects. If it does not drop and
  `traced_mb` stays high, a reference keeps the memory alive and `sites[0]` names the allocating line.

**Cloud Run throttles CPU outside requests** (default request-based billing). The sampler then runs in
bursts (observed: 10–24 s gaps at a 5 s interval) and a census, which needs tens of seconds of CPU after the
turn's response has gone out, never completes. For a census window deploy with `--no-cpu-throttling`
(`gcloud run services update alek-bot-dev --no-cpu-throttling`, instance-based billing while it is set) and
revert with `--cpu-throttling` afterwards.

`traced_mb` counts only Python allocations made after tracing started; `usage_mb` is the whole container, so the
two differ by the interpreter, native libraries and the tracing bookkeeping itself.

## Not covered yet

`memory.pressure` (a census on crossing 85% of the limit instead of after a long turn), payload size for
non-OpenAI providers, named contributors for caches, and leftover `/tmp` files created by opening a drive image or PDF
(the `tmp_mb` figure shows the effect; the files are not yet deleted).
