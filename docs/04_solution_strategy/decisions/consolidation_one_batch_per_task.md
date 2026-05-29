# Decision: One consolidation batch per Cloud Task

**Status:** Adopted — F8.6 closed
**Date:** 2026-05-29
**Context:** Inspection finding F8.6 — `docs/reviews/ARCHITECTURE_INSPECTION_FOLLOWUP.md`

## Decision

`ConsolidationService.process_user_batches` with `max_batches=1` (overflow path)
is the **intended production shape**, not a zombie. Each Cloud Task processes
exactly one batch and re-enqueues a new task if pending batches remain
(`worker_handler.py::_handle_consolidation`).

## Why this is the fix, not the artifact

Cloud Run throttles CPU to ~5% after the HTTP response is sent. A long-running
HTTP request that loops through many batches would be CPU-starved on later
batches, producing the original 74–180s `find_nearest` latencies that motivated
the session-5 (2026-02-24) refactor.

The shape — one batch per HTTP request, each request being its own Cloud Task
POST `/worker` — ensures every batch executes on a fresh Cloud Run instance with
full CPU allocation. This is the **resolution** of the CPU-throttling era, not
its leftover.

## Evidence

- `worker_handler.py:232` docstring: *"One batch per HTTP request → each Cloud
  Task gets full CPU on Cloud Run."*
- `consolidation_service.py:28-30` docstring: documents the two modes — overflow
  (`max_batches=1`, re-enqueue) vs manual `$consolidate` (`max_batches=None`,
  process all).
- Session-5 memory (2026-02-24) explicitly identifies CPU throttling as the
  root cause and Cloud Tasks dispatch as the fix.

## Why the audit interview reading was misleading

Author Q answer characterized the pattern as a "zombie from active experimentation
period". The interview framing conflated two adjacent patterns from the same
debugging window: the abandoned `asyncio.create_task` approach (real zombie) and
the surviving Cloud Tasks dispatch (real fix). The latter has been the production
shape since 2026-02-24 and is documented at the call-site.

## Rejected alternatives

- **Remove `max_batches` parameter, always process all pending in one task.**
  Reintroduces the CPU-throttling failure mode. The `find_nearest` semaphore
  guard (`firestore_repo.py::_FIND_NEAREST_SEMAPHORE`) is a quota limiter, NOT
  a latency fix for throttled CPU — explicitly noted in session-5 memory.
- **Process N>1 batches per task as a middle ground.** Same failure mode, just
  starts later. Each batch beyond the first hits degraded CPU.
- **Replace Cloud Tasks dispatch with persistent worker.** Architectural change
  out of scope; current shape works at solo-use scale.

## Trigger to revisit

- Cloud Run pricing model changes such that idle-period CPU is no longer
  throttled (e.g. migration to Cloud Run Jobs or always-on instances).
- Concrete throughput requirement that exceeds one-batch-per-task at scale
  (current overflow rate is well below that threshold).
