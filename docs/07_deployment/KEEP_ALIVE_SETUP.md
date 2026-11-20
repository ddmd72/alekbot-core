<!-- living-doc
covers:
  scripts/infrastructure/setup-keep-alive.sh
-->

# Keep-Alive

## Why

The main service deploys with `--min-instances=0` (`cloudbuild-dev.yaml`, step `deploy-main`), so an idle instance is
shut down and the next request pays a cold start and rebuilds in-memory state. Scale-to-zero costs nothing; a
Cloud Scheduler job that sends `GET /health` every 10 minutes keeps one instance alive for cents a month.
`min-instances=1` does the same for a real monthly cost. The owner's choice is the ping.

Keep-alive prevents instance shutdown only. It does not cure CPU throttling: Cloud Run throttles CPU once a response
is sent, so work started with `asyncio.create_task` in a route stalls even on a warm instance. Background work goes
through Cloud Tasks, a separate request with full CPU (`CLAUDE.md` § Traps, `docs/07_deployment/SCHEDULERS.md`).

## How it is set up

- **Live job: build-managed.** `scripts/infrastructure/cloudbuild_scheduler_jobs.sh` (step `scheduler-jobs` of
  `cloudbuild-dev.yaml`) updates or creates the job `alek-bot-dev-keep-alive` on every deploy; it is not set up by
  hand. Its schedule, deadline and the other jobs are in [SCHEDULERS.md](SCHEDULERS.md).
- **Target:** `GET /health`, unauthenticated, answers `{"status": "healthy", "mode": "http"}`
  (`src/web/main_service_routes.py::create_main_service_blueprint`).
- **The voice relay has its own keep-alive** (a different job, through the main service); see SCHEDULERS.md.

## Manual setup (a standalone service)

`scripts/infrastructure/setup-keep-alive.sh` creates or updates a job named `alek-bot-keep-alive` against any
service URL. It reads `GCP_PROJECT_ID`, `SCHEDULER_LOCATION` and `CLOUD_RUN_URL` from the environment (its defaults are
placeholders, so always export all three), then runs the job once and prints its description.

```bash
SERVICE_URL=$(gcloud run services describe <service> --region=<region> --format='value(status.url)')
GCP_PROJECT_ID=<project-id> SCHEDULER_LOCATION=<region> CLOUD_RUN_URL="$SERVICE_URL" \
  ./scripts/infrastructure/setup-keep-alive.sh
```

Trap: its job name differs from the build-managed one. Running it against the live service adds a second pinger
next to `alek-bot-dev-keep-alive`; use it only for a service the build does not manage.

## Checking and fixing

- Job exists and ran: `gcloud scheduler jobs describe <job> --location=<region>`; force a run with
  `gcloud scheduler jobs run <job> --location=<region>`.
- Execution results: Cloud Logging, `resource.type=cloud_scheduler_job` and `resource.labels.job_id=<job>`.
- 4xx/5xx in those logs: call `<service-url>/health` by hand; a healthy service returns the body above.
- Instance count on the Cloud Run metrics page should stay at 1 between requests.

## Alternative: min-instances

`make start` sets `--min-instances 1` on the service and `make stop` sets it back to 0 (`Makefile`). Use it when the
ping is not enough; it is billed for the instance while idle.

## Removal

`gcloud scheduler jobs delete <job> --location=<region>`. For the build-managed job also remove its block from
`scripts/infrastructure/cloudbuild_scheduler_jobs.sh`, or the next deploy recreates it.
