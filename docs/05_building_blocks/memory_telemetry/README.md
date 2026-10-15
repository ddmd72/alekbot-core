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

## Not covered yet

`memory.pressure` (object-type census on crossing 85%), payload size for non-OpenAI providers, named
contributors for caches, and leftover `/tmp` files created by `open_file_from_drive` (the `tmp_mb` figure
shows the effect; the files are not yet deleted).
