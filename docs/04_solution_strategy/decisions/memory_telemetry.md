# Memory telemetry — every process, every work unit, queryable for 90 days

Status: accepted by the owner 2026-10-15 (sink, retention). Slice 1 in progress on `feat/memory-telemetry`.

## Why

2026-10-15: `alek-bot-dev` was killed twice for exceeding 1 GiB during one long Smart turn that opened
six PDFs from the user's drive (17 MB of PDF). Cloud Run's own metric (p99 per minute, per revision)
could say *that* memory grew, not *what* held it, *which* work unit was running, or *for how long*. The
investigation that followed took hours and ended with two competing hypotheses (Logfire content capture
of base64 attachments vs. the request build vs. files left in `/tmp`) that one A/B could not separate.
The owner's decision: stop hunting one cause; log memory for all processes so any period can be
analysed afterwards.

## Facts established by probe (2026-10-15, throwaway Cloud Run Job on our image, gen2)

- `/sys/fs/cgroup/memory/memory.usage_in_bytes` and `memory.limit_in_bytes` (cgroup **v1** layout) are
  readable; the v2 files (`memory.current`, `memory.max`) are absent.
- `/tmp` is an overlay whose upper layer lives in memory: writing a 300 MB file raised
  `usage_in_bytes` from 330 MB to 646 MB while the process RSS stayed at 317 MB. **RSS alone misses
  `/tmp`; container usage is the number Cloud Run kills on.**
- Jobs cannot run gen1. The main service's environment (gen1 vs gen2) is unverified; the service
  logs a `memory.boot` record at start with what it can read, so both layouts are handled and the
  answer lands in the logs.
- `FileManagementAgent._fetch_binary` and the drive `open_file_from_drive` path create
  `tempfile.mkstemp` files that no code deletes (only `ConversationHandler.temp_files`, which holds
  user uploads, is cleaned). They count against memory until the instance dies.

## Decision

Structured JSON lines on stdout (already ingested by Cloud Logging through `StructuredLogHandler`),
routed by a **log sink to BigQuery** into a dedicated dataset with 90-day partition expiry.
No new write path in code, identical for the main service, indexer, relay and the research Job.

Four record kinds, all with `event`, `svc`, `rev`, `boot` (random per process start):

1. `memory.sample` — every 15 s (every 1 s above 75% of the limit). Container usage / limit / cache,
   main-process RSS and peak, `/tmp` size, child processes (name, RSS, age), asyncio tasks, threads,
   open fds, the active work units, and named *contributors* (counters that components register:
   bytes of attachments in flight, cache sizes).
2. `memory.scope` — one per finished work unit (`worker_task`, `chat_turn`, `agent_execution`,
   `subprocess`): `trace_id`, duration, container usage at start / end / peak, delta, the most
   scopes that overlapped it. Container usage is process-wide, so a scope's delta is **an upper bound
   shared with whatever ran concurrently**, not an exact attribution; `concurrent` and the overlapping
   sample rows are what make an honest estimate possible.
3. `memory.payload` — one per OpenAI request that carries attachments: file parts and base64 megabytes
   on the wire. This is the figure the 2026-10-15 investigation lacked: what a long turn re-sends on every
   round. Other providers: later.
4. `memory.pressure` (slice 2) — on crossing 85%: contributors, top children, scopes and the top
   Python object types by count, with a cooldown. This is the black box for the next OOM.

## Layering

`domain/memory_telemetry.py` (records, thresholds, flattening — pure) · `ports/memory_probe.py`
(the OS boundary) · `adapters/proc_memory_probe.py` (`/proc` + cgroup v1/v2, no new dependency) ·
`services/memory_telemetry_service.py` (sampler loop, scope accounting, emits via `logger`) ·
`utils/memory_scope.py` (context manager the handlers and agents call; a no-op until the service
starts, like `set_request_context`) · wiring in `main.py`, `indexer_main.py`, `relay_main.py`,
`job_main.py`. Env knobs (`os.getenv`, optional): `MEMORY_TELEMETRY` (default on),
`MEMORY_SAMPLE_INTERVAL_S`.

## Alternatives rejected

- **Cloud Monitoring custom metrics** — per-series pricing, no per-work-unit breakdown.
- **Logfire metrics** — the project has no metrics pipeline; 30-day retention; one more exporter
  holding data in memory in the very process being measured.
- **Direct BigQuery adapter** — a second write path to build, retry and keep alive in four entry
  points, for what the log sink does for free. Revisit if log ingestion ever becomes a cost.
- **`tracemalloc` always on** — records the allocation site of every object (exact, but the process
  runs roughly 1.5–3× slower and holds the bookkeeping in memory). Reserved for a flag-gated
  investigation window, not permanent.

## Cost

About 6k sample rows per service per day (~2 MB). Log ingestion and streaming into BigQuery at that
size is cents per month; 90-day partition expiry bounds storage.

## Sink setup (outside the repo; owner step)

See `docs/07_deployment/README.md` § Memory telemetry sink.
