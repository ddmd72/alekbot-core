<!-- living-doc
covers: none
-->

# Keep-Alive

## Why

The main service deploys with `--min-instances=0` (`cloudbuild-dev.yaml`, step `deploy-main`), so an idle instance is
shut down and the next request pays a cold start and rebuilds in-memory state. Scale-to-zero costs nothing; a
Cloud Scheduler job that sends `GET /health` on a short schedule keeps one instance alive for cents a month.
`min-instances=1` does the same for a real monthly cost. The owner's choice is the ping.

Keep-alive prevents instance shutdown only. It does not cure CPU throttling: Cloud Run throttles CPU once a response
is sent, so work started with `asyncio.create_task` in a route stalls even on a warm instance. Background work goes
through Cloud Tasks, a separate request with full CPU (`CLAUDE.md` § Traps, `docs/07_deployment/SCHEDULERS.md`).

## How it is set up

- **Live job: build-managed.** `scripts/infrastructure/cloudbuild_scheduler_jobs.sh` (step `scheduler-jobs` of
  `cloudbuild-dev.yaml`) updates or creates the job `alek-bot-dev-keep-alive` on every deploy; it is not set up by
  hand. Its schedule and attempt deadline are set in that script; the job list is in [SCHEDULERS.md](SCHEDULERS.md).
- **Target:** `GET /health`, unauthenticated, answers 200 with a healthy status body
  (`src/web/main_service_routes.py::create_main_service_blueprint`).
- **The voice relay has its own keep-alive** (a different job, through the main service); see SCHEDULERS.md.

## Checking and fixing

- Job exists and ran: `gcloud scheduler jobs describe <job> --location=<region>`; force a run with
  `gcloud scheduler jobs run <job> --location=<region>`.
- Execution results: Cloud Logging, `resource.type=cloud_scheduler_job` and `resource.labels.job_id=<job>`.
- 4xx/5xx in those logs: call `<service-url>/health` by hand; a healthy service answers 200.
- Instance count on the Cloud Run metrics page should stay at 1 between requests.

## Alternative: min-instances

`make start` sets `--min-instances 1` on the service and `make stop` sets it back to 0 (`Makefile`). Use it when the
ping is not enough; it is billed for the instance while idle.

## Removal

`gcloud scheduler jobs delete <job> --location=<region>`. For the build-managed job also remove its block from
`scripts/infrastructure/cloudbuild_scheduler_jobs.sh`, or the next deploy recreates it.
