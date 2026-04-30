# Decision: Cloud Tasks vs Cloud Run Jobs

**Status:** Adopted
**Date:** 2026-04-30
**Context:** [docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md] § 2 (non-goals)

## Decision

| Workload | Mechanism |
|---|---|
| Reminder fire | Cloud Tasks → `/worker` (`execute_reminder`) |
| Daily email review | Cloud Tasks → `/worker` (`daily_email_review`) |
| Consolidation | Cloud Tasks → `/worker` (`consolidation`) |
| Async document/PDF/HTML generation | Cloud Tasks → `/worker` (`agent_execution`) |
| **Deep Research execution** | **Cloud Run Job** (already so) |
| Future long-running batch (≥ 30 min, periodic) | Cloud Run Job |

## Why Cloud Tasks (not Jobs) for notify/reminder/consolidation

- **Cold start tax.** Jobs spin a fresh container per execution
  (5–30 s). Notify/reminder/consolidation fire 10–100×/day across
  all users. Cumulative cold-start overhead per day exceeds the
  CPU savings.
- **Prompt cache.** Anthropic prompt cache TTL is 5 min. Cold Job
  start = cold prompt cache = lose ~50% of input-token savings on
  Claude. Smart's `PROMPT_CACHE_BOUNDARY` architecture exists
  precisely to retain cached prefixes; Jobs would defeat it.
- **Biographical cache.** 24 h TTL, per Cloud Run instance.
  In-process caching across many short tasks beats per-Job re-fetching.
- **30-min timeout is sufficient** for these workloads (post-RFC).
  `DAILY_DIGEST` SLA is 25 min. Reminder is 10 min. Consolidation is
  seconds–minutes. None exceed the Cloud Tasks → Cloud Run request
  ceiling.

## Why Jobs for Deep Research

- **5-hour wall-clock budget.** Cloud Run request limit is 30 min;
  insufficient.
- **Dedicated container, no CPU contention** with the main service.
- **Already implemented** via `JobRunnerPort` + `CloudRunJobsAdapter`
  + `job_main.py`.

## Why Jobs for future long-running batch

- Mass embedding regeneration, eval suites, periodic recompute
  pipelines: these are exactly the shape Jobs are built for.
- Trigger via Cloud Scheduler → Cloud Run Job (no HTTP intermediary).

## Revisit triggers

- Smart latency under DAILY_DIGEST regularly exceeds 25 min →
  consider migrating that path to Jobs.
- Active user count grows beyond what 1 vCPU on the main service
  handles → first scale `concurrency` and instance count, only then
  consider Jobs.
- New workload appears that needs > 30 min OR genuine CPU isolation
  (e.g. local ML inference) → Jobs.

## Trade-off acknowledged

Cloud Tasks + main service shares a single Cloud Run instance pool.
A storm of background notifications can starve interactive traffic
(or vice versa). Mitigation today: 1 vCPU is the default; auto-scale
multiplies instances under load. If contention becomes measurable,
the next move is `--concurrency` tuning + per-task-type queues with
rate limits — not migration to Jobs.
