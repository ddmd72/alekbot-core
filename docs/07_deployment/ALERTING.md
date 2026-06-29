# Alerting (dev)

GCP Monitoring alert policies for the `alek-bot-dev` Cloud Run service. **gcloud-managed, NOT in
git** — this doc describes what exists and why; the live source of truth is the GCP project.

All policies route to one Slack notification channel **#alerts-dev** (channel id stored in GCP /
`.env`, never committed). Each has `notificationRateLimit` 1/hour + `autoClose` 24h.

## Policies

1. **Errors-dev** (pre-existing) — `cloud_run_revision` + `severity>=ERROR`. Catches exceptions
   *inside* a running task.
   **Blind spot:** a task that never *runs* (e.g. a `/worker` 401, logged as WARNING) produces no
   ERROR, so this policy alone misses "task didn't start" failures.
2. **Worker non-2xx (legit callers) - dev** (2026-06-03) — `/worker` non-2xx from a
   `Google-Cloud-*` user-agent OR any 5xx. Catches Cloud Tasks / Scheduler being rejected or
   erroring at the HTTP layer; excludes anonymous-scanner 401 noise (the OIDC gate 401s random
   POSTs by design — see `decisions/worker_oidc_and_docx_sandbox.md`).
3. **Cloud Scheduler job failures - dev** (2026-06-03) — `cloud_scheduler_job` failures. Covers the
   "scheduled task never fired" gap that policy 1 cannot see.

## Why policies 2–3 exist

Policy 1 only fires on errors *within* a task. The OIDC gate on `/worker` returns 401 (logged
WARNING) when a caller is rejected, and a scheduler that never invokes the service emits nothing to
the revision logs — both are silent to an ERROR-only policy. 2 and 3 close that gap at the HTTP and
scheduler layers respectively. See also `decisions/` and the enumerate-callers-before-gating lesson.
