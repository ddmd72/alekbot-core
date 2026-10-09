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
4. **Sensitive-path 2xx tripwire - dev** (2026-10-07) — `cloud_run_revision` + a 2xx response to a
   path vulnerability scanners probe for (`.env`, `credentials*`, `wp-*`, `.php`,
   `/proc/self/environ`, `aws-exports.js`, `appsettings.json`). The weekly scan volume on these
   paths is pure noise (verified 100% 404, excluded from the `_Default` sink — see the
   `scanner-404-noise` exclusion) — this fires only if one of them is ever actually served.
5. **Cabinet sign-in rejected - dev** (2026-10-09) — `textPayload:"OAuth sign-in rejected"` on
   `alek-bot-dev`. The whitelist gate refused a Google account at `/auth/callback` or
   `/auth/link-oauth`; the log line names the reason (not whitelisted / email not verified / no
   email) and the email. Matched on the app log, not on HTTP 403, so it covers both routes and
   carries the reason. CSRF state is checked before the gate, so scanners cannot reach it.
6. **New user registered (tripwire) - dev** (2026-10-09) — `textPayload:"Registering new user"`.
   Only whitelisted emails can register, so this is rare; one you did not expect means the gate
   was bypassed. Same idea as policy 4: alert on the attempt *succeeding*.
   See `decisions/cabinet_oauth_whitelist_gate.md` for both.

## Why policies 2–3 exist

Policy 1 only fires on errors *within* a task. The OIDC gate on `/worker` returns 401 (logged
WARNING) when a caller is rejected, and a scheduler that never invokes the service emits nothing to
the revision logs — both are silent to an ERROR-only policy. 2 and 3 close that gap at the HTTP and
scheduler layers respectively. See also `decisions/` and the enumerate-callers-before-gating lesson.

## Logging exclusions

`_Default` sink exclusion **`scanner-404-noise`** (2026-10-07): drops `run.googleapis.com/requests`
entries with `httpRequest.status=404` from storage — background vulnerability-scanner traffic
(verified zero 2xx hits over a 7-day sample, `docs/reviews/PROD_LOG_AUDIT_FOLLOWUP.md` C-17).
Anything that stops being a 404 is not excluded, and is independently covered by policy 4 above.
