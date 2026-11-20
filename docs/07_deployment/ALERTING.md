<!-- living-doc
covers:
  src/ports/alert_sink.py
-->

# Alerting (dev)

GCP Monitoring alert policies on the Cloud Run services. **gcloud-managed, not in git:** the live GCP project is the
source of truth for filters, rate limits and auto-close; this doc says what each policy is for, what trips it and what to
do. All policies route to one Slack notification channel (`#alerts-dev`; its id lives in GCP / `.env`, never in git).

Two services are watched: the main service and the drive indexer. The two log-based "errors" and "worker" policies
filter on both service names, so a new Cloud Run service needs its name added to both filters. The indexer's staleness
alert is separate: the scheduler fan-out posts it through `src/ports/alert_sink.py::AlertSinkPort` (implemented by
`src/adapters/slack/webhook_adapter.py::SlackWebhookAdapter`, wired in `src/composition/operations_setup.py`); see
`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md` §12.1.

## Policies

| # | Policy | Trips on | First reaction |
|---|---|---|---|
| 1 | Errors-dev | a log entry with severity ERROR or higher on either service | read the entry (`make fetch-logs`, see `CLAUDE.md` § Debugging Cloud Run) |
| 2 | Worker non-2xx (legit callers) | a `/worker` non-2xx from a `Google-Cloud-*` user agent, or any 5xx | Cloud Tasks / Scheduler is being rejected or the handler fails at HTTP level: check the OIDC identity and the handler logs |
| 3 | Cloud Scheduler job failures | a `cloud_scheduler_job` failure | a scheduled task never fired; see `docs/07_deployment/SCHEDULERS.md` |
| 4 | Sensitive-path 2xx tripwire | a 2xx answer to a path scanners probe (`.env`, `credentials*`, `wp-*`, `.php`, `/proc/self/environ`, and similar) | something sensitive is being served: treat as a leak, find the route that answered |
| 5 | Cabinet sign-in rejected | log text `OAuth sign-in rejected` on the main service | the line names the reason (no email, email not verified, not whitelisted) and the email; decide whether to whitelist or ignore |
| 6 | New user registered (tripwire) | log text `Registering new user` | only whitelisted emails can register, so an unexpected one means the gate was bypassed |

Why these six exist:
- **Policy 1 alone has a blind spot.** It sees only errors raised inside a running task. A rejected `/worker` caller
  is logged as WARNING (`src/web/main_service_routes.py`, "Rejected unauthenticated /worker request", 401), and a
  scheduler that never invokes the service writes nothing to the revision logs. Policies 2 and 3 close that gap at the
  HTTP and scheduler layers.
- **Policy 2 excludes anonymous scanners on purpose.** The OIDC gate answers 401 to random POSTs by design
  (`docs/04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md`); only Google callers or 5xx page.
- **Policies 4 and 6 alert on an attempt succeeding**, not on the attempt. Policy 4 pairs with the log exclusion below.
- **Policy 5 matches the app log, not HTTP 403**, so it covers both gate routes (`/auth/callback`, `/auth/link-oauth`,
  `src/web/oauth_app.py`) and carries the reason. The CSRF state is checked before the gate, so scanners cannot reach
  it. Policies 5 and 6: `docs/04_solution_strategy/decisions/cabinet_oauth_whitelist_gate.md`.

## Traps

- **Log text and severity are an interface.** Policies 5 and 6 match literal text and policy 1 matches severity, so
  changing the wording or level of these lines silently disables the alert:
  `src/services/authentication_service.py::AuthenticationService._require_allowed_email` (`OAuth sign-in rejected`, kept
  verbatim by its docstring) and the `Registering new user` line in the same file.
- **A text filter reads `jsonPayload.message`, never `textPayload`.** On Cloud Run the logger writes one JSON object per
  line (`docs/07_deployment/LOGGING.md`), so the message lands in `jsonPayload.message` and `textPayload` stays empty: a
  `textPayload:"..."` condition is valid, saves, and never trips.
- **A systemic failure must log at ERROR to page; a file's own failure must not.** The document-service path and the
  drive open path pick the level for this reason (`src/services/document_service.py`, `src/domain/document_read.py::SYSTEM_FAULT_STATUSES`).
  A new failure path that logs WARNING for a systemic cause never pages.

## Logging exclusion

The `_Default` sink exclusion `scanner-404-noise` drops `run.googleapis.com/requests` entries with
`httpRequest.status=404` from storage: background scanner traffic with no 2xx hits in the owner's sample. Anything that
stops being a 404 is not excluded and is covered by policy 4.
