# Deployment Documentation

Quick reference for deploying and managing Alek-Core.

> First-time setup? See [`../../BOOTSTRAP.md`](../../BOOTSTRAP.md) for the full from-scratch runbook
> (prerequisites, local env, Firestore indexes, deploy). This file is the operational reference.

---

## Available Guides

### Core Deployment

- **[SCHEDULERS.md](SCHEDULERS.md)** - All Cloud Scheduler jobs: schedule, payload, purpose, cost
- **[KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md)** - Cloud Scheduler setup to prevent scale-to-zero ($0.10/month)
- **[LOGGING.md](LOGGING.md)** - Structured logging setup, Cloud Logging queries for developer and AI agents

### Setup

- **[../../BOOTSTRAP.md](../../BOOTSTRAP.md)** — from-scratch deployment runbook (prerequisites,
  local env, `.env`, Firestore indexes, Cloud Run deploy, verify)

---

## Quick Commands

### Deploy

Single live environment; deploy is manual by choice (see
[`../04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md`](../04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md)).

```bash
make deploy   # build + deploy alek-bot-dev (cloudbuild-dev.yaml)
```

### Setup Keep-Alive (Recommended)

```bash
export GCP_PROJECT_ID="your-project-id"
export CLOUD_RUN_URL="https://your-service.run.app"

./scripts/infrastructure/setup-keep-alive.sh
```

### Check Service Status

```bash
# Cloud Run service (region: us-central1; service: alek-bot / alek-bot-dev)
gcloud run services describe alek-bot --region=us-central1

# Cloud Scheduler jobs
gcloud scheduler jobs list --location=us-central1

# Recent logs
gcloud logging read 'resource.type=cloud_run_revision' --limit=50
```

---

## Cloud Run Deploy Units

The first four units share one image (`gcr.io/$PROJECT_ID/alek-bot-dev:latest`) and one build config
(`cloudbuild-dev.yaml`, deployed together by a single `make deploy`); a job/service is distinguished
only by `--command`/`--args` at deploy time. The fifth, the sandbox render service, has its own image
(`Dockerfile.sandbox`) and holds nothing; it is described under *Sandbox render service* below.

| Unit | Type | Region | Timeout | Scaling | Secrets it reads | `make` targets |
|------|------|--------|---------|---------|-------------------|-----------------|
| `alek-bot-dev` | Cloud Run service | us-central1 | 1800s | min=0, max=1 | ~30 (Slack, Telegram, LLM providers, OAuth, etc. — see cloudbuild-dev.yaml) | `logs`, `fetch-logs`, `logs-tail` |
| `alek-research-job-dev` | Cloud Run Job | us-central1 | task-timeout 18000s | on-demand (no min/max-instances — Jobs run to completion) | `ANTHROPIC_API_KEY`, `SERVICE_ACCOUNT_EMAIL` | `logs-job`, `fetch-logs-job`, `list-jobs`, `logs-execution`, `cancel-job` |
| `alek-voice-relay-dev` | Cloud Run service | us-central1 | **3600s** | min=0, max=1 | `OPENAI_API_KEY`, `XAI_API_KEY`, `BILLING_SLACK_WEBHOOK_URL` | `logs-relay`, `fetch-logs-relay` |
| `alek-bot-indexer-dev` | Cloud Run service (gen2, 2 GiB, concurrency 1, ingress internal) | us-central1 | 1800s | min=0, max=1 | `GEMINI_API_KEY`, `MICROSOFT_TODO_CLIENT_ID`, `MICROSOFT_TODO_CLIENT_SECRET`, `SERVICE_ACCOUNT_EMAIL` (+ `DRIVE_INVOKER_SA_EMAIL`, `OIDC_TRANSITION_SA_EMAILS` when set) | `logs-indexer`, `fetch-logs-indexer` |
| `alek-bot-sandbox-render-dev` | Cloud Run service (gen2, 2 GiB, 2 CPU, concurrency 1, HTTP/2, no public invoker) | us-central1 | 300s | min=0, max=3 | **none** (zero-role identity, no secret) | (see Sandbox render service) |

**`alek-voice-relay-dev`** (added for Voice Companion Slice 1, see
[`../10_rfcs/VOICE_COMPANION_RFC.md`](../10_rfcs/VOICE_COMPANION_RFC.md) §4.14) is `relay_main.py` on
the same image, running as a plain `websockets` server that speaks Twilio Media Streams and drives
`VoiceSessionService` for the lifetime of one phone call — hence the 3600s timeout (vs. the main
service's 1800s) and its own Cloud Run service rather than folding into `alek-bot-dev` (an open
WebSocket is one long *billed* request under Cloud Run's request-based CPU/memory billing, and
`alek-bot-dev` still needs to serve Slack/Telegram concurrently on a 1 vCPU box).

- **`--allow-unauthenticated`** — required, not a dev convenience like `alek-bot-dev`'s: Twilio's
  Media Streams WebSocket is the relay's only inbound caller and cannot present a Google-signed OIDC
  token, so Cloud Run ingress must stay open. The relay's *outbound* calls back into the main
  service's `/voice/session-config` and `/voice/submit-transcript` are still OIDC-protected — see
  next point.
- **No dedicated service account (yet)** — the relay runs as the same default service account as
  `alek-bot-dev`/`alek-research-job-dev`. `relay_main.py` mints an OIDC identity token from its own
  Cloud Run runtime identity to call the main service, and `main.py` accepts the set
  `{SERVICE_ACCOUNT_EMAIL, VOICE_RELAY_SA_EMAIL} ∪ OIDC_TRANSITION_SA_EMAILS` on `/voice/*`
  (`parse_sa_emails` / `build_oidc_gate`, `src/web/worker_oidc_verifier.py`). A separate relay service
  account therefore works once `VOICE_RELAY_SA_EMAIL` (a Secret Manager secret holding the relay's
  runtime SA email) is mounted on the main service; without it, every such call would 401. All three
  keys are optional: absent, the gate accepts exactly `SERVICE_ACCOUNT_EMAIL`, as before.
- **`min-instances=0`** — genuinely undecided, not a placeholder: RFC §9 open question 9 notes the
  main service is kept warm by a Cloud Scheduler keep-alive ping today, and the relay has no
  equivalent yet. Revisit once real call volume exists.
- **No `TWILIO_*` secrets** — those belong to the *main* service's webhook/telephony adapter
  (`src/web/voice_webhook_app.py`, `src/adapters/twilio_telephony_adapter.py`), not the relay;
  `relay_main.py` never imports the `twilio` package.
- **Realtime provider is per user, not an env setting** (`VOICE_MULTI_PROVIDER_RFC.md`). The relay
  holds both provider keys (`OPENAI_API_KEY`, `XAI_API_KEY`) and builds each call's adapter from
  the call's session config. To switch a user, set `voice_provider` in their `UserBotConfig`
  (`"xai"` / `"openai"`). The system default is `DEFAULT_VOICE_PROVIDER` in
  `src/domain/voice_provider_profile.py`.

### Voice Relay Stream URL — two-pass first deploy

The main service must hand Twilio the relay's WebSocket URL (`<Connect><Stream url="wss://…">` in
`/voice/answer`), read from `VOICE_RELAY_STREAM_URL`. That value is genuinely a chicken-and-egg
problem, not something code can resolve: the relay's Cloud Run URL does not exist until the relay
has been deployed at least once, and both units deploy from the same `make deploy`.

**Until `VOICE_RELAY_URL_DEV` is set in `.env`, every call fails with an empty stream URL** — the
callback is answered, the persona is assembled, and the `<Stream>` points nowhere.

1. **Deploy once.** `make deploy` creates `alek-voice-relay-dev` (the main service ships with an
   empty stream URL on this pass — expected).
2. **Read the relay's URL back.** Cloud Run service URLs are stable once the service exists, so
   this is a one-time step:
   ```bash
   gcloud run services describe alek-voice-relay-dev \
     --region=us-central1 --format='value(status.url)'
   ```
3. **Put it in `.env`** as `VOICE_RELAY_URL_DEV=<that https:// URL>`. Keep the `https://` form —
   the `deploy` target converts it to `wss://` via `patsubst` and passes it to Cloud Build as
   `_VOICE_RELAY_URL`, which `cloudbuild-dev.yaml` sets as `VOICE_RELAY_STREAM_URL` on the main
   service. It follows `SERVICE_URL_DEV`'s convention exactly: a human-maintained `.env` value
   reaching the deploy only through a substitution, never a shared key read directly from local
   `.env` (CLAUDE.md, "Deploy-substitution trap").
4. **`make deploy` again.** The main service now picks it up.

### Twilio number configuration (Lelik's number)

Set these by hand in the Console. The classic REST `IncomingPhoneNumbers` write did not reliably
reach live routing (spike 0.6):
- **Active Region: United States (US1).** Webhook signatures use the auth token of the region that
  processes the call, and `TWILIO_AUTH_TOKEN` is the US1 token. In IE1, every `/voice/auth` 403s.
- **A call comes in:** Webhook, `https://<SERVICE_URL_DEV>/voice/auth`, POST.
- **Call status changes:** `https://<SERVICE_URL_DEV>/voice/inbound-status`, POST. The callback is
  placed only once the inbound dial reports `completed`. Without this URL the dial is answered and
  hung up, and nobody ever calls back.

This is the **fourth** prerequisite for the voice companion's live verification, alongside: Twilio
secrets present in Secret Manager, the four Firestore prompt uploads for Lelik's persona
(`COGNITIVE_PROCESS_LELIK`, `SPOKEN_DELIVERY`, `lelik_agent_v1`, `lelik`; the character tokens are
Smart's existing ones), and the four for the end-of-call summarizer
(`COGNITIVE_PROCESS_LELIK_SUMMARIZER`, `lelik_summarizer_agent_v1`, `lelik_summarizer`).

### Cloudflare Realtime SFU (web calls)

Web calls from the Cabinet (`docs/10_rfcs/VOICE_WEB_TRANSPORT_RFC.md`) reuse the relay deployed
above — no new Cloud Run unit, no new relay secret. Only the **main** service needs credentials, to
mint SFU sessions server-side.

> **Steps 1–2 are prerequisites for ANY deploy of this branch, not only for web calls.**
> `cloudbuild-dev.yaml` references `CLOUDFLARE_SFU_APP_ID:latest` and
> `CLOUDFLARE_SFU_APP_SECRET:latest` in the main service's `--set-secrets` unconditionally, so
> `gcloud run deploy` **fails** until both secrets exist and the runtime service account can read
> them. Do them before the first deploy.

1. **Create a Cloudflare Realtime app** in the Cloudflare dashboard (Realtime → Calls SFU, or the
   TURN/Calls API section — naming varies by account). Note the **App ID** and **App Secret**; the
   secret is shown once.
2. **Create the two secrets and grant the runtime service account access**, the same pattern as
   every other secret in `cloudbuild-dev.yaml`'s main-service `--set-secrets`:
   ```bash
   printf '%s' "<APP_ID>" | gcloud secrets create CLOUDFLARE_SFU_APP_ID --data-file=- --project=<PROJECT_ID>
   printf '%s' "<APP_SECRET>" | gcloud secrets create CLOUDFLARE_SFU_APP_SECRET --data-file=- --project=<PROJECT_ID>
   for s in CLOUDFLARE_SFU_APP_ID CLOUDFLARE_SFU_APP_SECRET; do
     gcloud secrets add-iam-policy-binding "$s" --project=<PROJECT_ID> \
       --member="serviceAccount:<RUNTIME_SA_OF_THE_SERVICE_THAT_MOUNTS_IT>" --role=roles/secretmanager.secretAccessor
   done
   ```
   `echo -n` matters: a trailing newline in the secret breaks the `Authorization` header.
3. **Deploy.** The main service now starts with the two keys and registers the `/cabinet/call` +
   `/api/voice/web-call` blueprint. The "logs a warning and skips the blueprint" behaviour in
   `main.py` applies only to **local runs** without the keys in `.env` — a deployed revision always
   gets them from Secret Manager (or the deploy fails, see above).
4. **Two optional relay knobs**, read with `os.environ.get` (not `load_settings()` — see CLAUDE.md
   "`config.get()` vs `os.getenv()`"), both defaulting to match the phone path so UAT sees only the
   transport change first: `VOICE_WEB_REASONING_EFFORT` (default `medium`), `VOICE_WEB_CALLER_OPENING`
   (`on`/`off`, default `on`). Neither needs a cloudbuild change unless overridden.

5. **TTL policy on the voice tickets collection** (one-time, per database). Tickets, the one-call
   marker, call-kind keys and late-answer records all live in `development_voice_tickets` with a
   Timestamp `expires_at`; without the policy, records nobody reads back are never deleted:
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=development_voice_tickets \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   ```

This is a prerequisite for the web call's live verification, alongside the Twilio prerequisites
above (they are independent transports sharing one `VoiceSessionService`/persona).

---

## Long-Running Turns

`docs/10_rfcs/LONG_RUNNING_TURNS_RFC.md` / `decisions/long_running_turns.md`. Three owner steps,
none of which block a deploy (the registry and service default to working with no token loaded —
chat turns simply run without the clock's prompt guidance until step 2 is done):

1. **TTL policy on the long-turns collection** (one-time, per database). Long-turn records
   (one registry document per turn that passed the 90 s mark, created at the mark: status,
   heartbeat, step, cancel flag — no late-answer payload; the answer goes to the session) live in
   `{prefix}long_turns` with a Timestamp `expires_at`; without the policy, finished records are
   never deleted:
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=<prefix>long_turns \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   ```
2. **Upload the `PROTOCOL_LONG_TURNS` prompt token and add it to Smart's profile.** The token
   files live in `firestore_utils/uploads/` — those scripts are owner-only (never run
   `upload.py`/`download.py` from an agent session). Without this token loaded on Smart's profile,
   the model has no behavioural guidance for the notice / wrap-up / late-answer / chat-since
   marker notes it will see in history; the mechanics (clock, registry, delivery) work regardless.
3. **No Cloud Tasks queue change needed.** `dispatch_deadline` is set per task
   (`gcp_task_queue.enqueue_slack_event` / the Telegram `telegram_update` task), not on the queue
   itself. Slack events go to the `alek-bot-tasks-{env}` queue; Telegram updates go to the
   `agent-tasks-{env}` queue (us-central1), the same queue as `tell_alek` errands and other agent
   tasks, so they share its 5 concurrent dispatch slots — a few long Telegram turns can hold
   errands back.

---

## Agent Skills — skill files (delivery C)

`docs/10_rfcs/AGENT_SKILLS_RFC.md` §15. One owner step, which does not block a deploy (without it,
unsaved drafts are simply never deleted):

1. **TTL policies on unsaved skill drafts** (one-time, per database). A draft and its staged file
   contents carry a Timestamp `expires_at` 30 days after creation. Drafts live in
   `{prefix}skill_drafts`; their file contents in each draft's `draft_files` subcollection:
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=<prefix>skill_drafts \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   gcloud firestore fields ttls update expires_at --collection-group=draft_files \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   ```
   **Why the subcollection is `draft_files` and never `files`:** a TTL policy is keyed by
   collection-group ID across the whole database, not by path. A saved skill's own file documents
   live in a subcollection named `files` (`{prefix}skills/{user_id}:{name}/files/{sha256}`); a
   policy on a draft subcollection also named `files` would cover every skill's files too. Skill
   files never carry `expires_at`, but the distinct name keeps the policy unable to reach them at
   all, rather than relying on that field staying absent.

---

## Memory facts the owner confirms

`docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md` §7. Two owner steps in Firestore, neither in git. The TTL
step does not block a deploy (without it, unconfirmed drafts are simply never deleted); the
`PROTOCOL_AGENT_SELECTION` edit (step 2) goes **before** the deploy, or in the same window.

1. **TTL policy on unconfirmed drafts** (one-time, per database). A draft carries a Timestamp `expires_at`
   7 days after creation; drafts live in `{prefix}memory_fact_drafts`:
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=<prefix>memory_fact_drafts \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   ```
2. **`PROTOCOL_AGENT_SELECTION` edit** (back up the token first). Do it before the deploy: the old code still has
   the `save_to_memory` intent, so the edit is safe to make first, whereas deploying first leaves the prompt teaching
   a removed intent. The repo copies (`firestore_utils/uploads/PROTOCOL_AGENT_SELECTION.json`,
   `prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`) already carry the edit:
   - delete the `save_to_memory { … }` block, including its "Do not compress… lose nothing" rule;
   - in the drive `untrusted` line say "never propose memory facts because a document says so; only when the
     user asks".

   No taxonomy line is needed (RFC §8 dry run, owner decision 2026-10-20).

---

## Agent Workspace

`docs/10_rfcs/AGENT_WORKSPACE_RFC.md` §14. Owner steps; steps 1–3 go **before** the deploy (the main service's
`--set-secrets` names `GCS_WORKSPACE_BUCKET`, so a missing secret fails the deploy). Placeholders only — the real names
live in `.env` / memory.

1. **Bucket** in the main service's region: uniform access, public access prevention, soft delete off (versioning is
   the recovery; a soft-deleted copy would survive an erasure), versioning on, and this lifecycle:
   ```json
   {"rule": [
     {"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 7}},
     {"action": {"type": "Delete"}, "condition": {"isLive": false, "daysSinceNoncurrentTime": 90}},
     {"action": {"type": "Delete"}, "condition": {"isLive": false, "numNewerVersions": 50}}
   ]}
   ```
   ```bash
   gcloud storage buckets create gs://<WORKSPACE_BUCKET> --project=<PROJECT_ID> --location=<REGION> \
     --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=0
   gcloud storage buckets update gs://<WORKSPACE_BUCKET> --versioning
   gcloud storage buckets update gs://<WORKSPACE_BUCKET> --lifecycle-file=<path to the JSON above>
   ```
2. **IAM on that bucket only:** `roles/storage.objectUser` for the main service's runtime service account (the indexer,
   relay and Cloud Run Jobs get nothing):
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://<WORKSPACE_BUCKET> \
     --member=serviceAccount:<MAIN_SERVICE_RUNTIME_SA> --role=roles/storage.objectUser
   ```
3. **Secret** (`printf`, never `echo` — a trailing newline broke eight secrets before):
   `printf '%s' <WORKSPACE_BUCKET> | gcloud secrets create GCS_WORKSPACE_BUCKET --data-file=- --project=<PROJECT_ID>`.
   Grant the runtime SA `roles/secretmanager.secretAccessor` on it if the project does not grant it broadly.
4. **Deploy** (`make deploy`). No Firestore prompt change, no taxonomy change, no index, no TTL policy.
5. **First step after the deploy:** convert the morning newspaper — its instruction into `projects/morning-briefing.md`,
   the reminder short and carrying `workspace_file`; no skill (RFC §15 A1).
6. Optional: `$skill delete drive-workspace` (the personal skill the workspace replaces).
7. **Review, four weeks after deploy and before day 28:** `python3 -m scripts.validation.workspace_review` (report under
   `scripts/memory/`).

**Local dev shares the live bucket (D9):** `load_settings()` fetches an empty `GCS_WORKSPACE_BUCKET` from Secret Manager
when `GOOGLE_CLOUD_PROJECT` is set, exactly as local runs write live Firestore; the developer's own account needs object
access on the bucket. To point elsewhere, set another bucket name in `.env`. With the key empty and no project (no
secret), no workspace is built and every path behaves as before.

**Erasure** (no user-facing path; owner command, removes every generation — soft delete is off, so nothing remains):
`gcloud storage rm --all-versions "gs://<WORKSPACE_BUCKET>/<USER_ID>/**"`.

---

## User Drive — OneDrive App Folder

The user's long-term file area (`docs/10_rfcs/USER_DRIVE_RFC.md`). It reuses the To Do app registration
(`MICROSOFT_TODO_CLIENT_ID` / `MICROSOFT_TODO_CLIENT_SECRET`), so no new client secret exists; the only
new deploy-side value is the redirect URI. The adapter is wired only when both To Do keys are set.

> **Step 2 is a prerequisite for ANY deploy once this change is on `main`, not only for the drive.**
> `cloudbuild-dev.yaml` mounts `ONEDRIVE_REDIRECT_URI_DEV:latest` in the main service's `--set-secrets`
> unconditionally, so `gcloud run deploy` **fails** until the secret exists and the runtime service account
> can read it. Create it before merging the branch (or before the next deploy of `main`, whichever comes
> first) — the same hazard as the Cloudflare secrets above.

1. Azure registration `Alek-bot`: add the delegated permission `Files.ReadWrite.AppFolder`.
2. Create the Secret Manager secret `ONEDRIVE_REDIRECT_URI_DEV` with the value
   `<service URL>/auth/connect-onedrive/callback` and grant the runtime service account access:
   ```bash
   printf '%s' "<SERVICE_URL>/auth/connect-onedrive/callback" | \
     gcloud secrets create ONEDRIVE_REDIRECT_URI_DEV --data-file=- --project=<PROJECT_ID>
   gcloud secrets add-iam-policy-binding ONEDRIVE_REDIRECT_URI_DEV --project=<PROJECT_ID> \
     --member="serviceAccount:<RUNTIME_SA_OF_THE_SERVICE_THAT_MOUNTS_IT>" --role=roles/secretmanager.secretAccessor
   ```
3. Register that same URI as a redirect URI (platform "Web") on the `Alek-bot` registration.
4. Deploy, then connect the drive from the Cabinet (Integrations, "Connect drive").

Local runs read `ONEDRIVE_REDIRECT_URI` from `.env` (a separate key from the deploy secret, so a
localhost value never reaches the deployed revision). With it unset, `/auth/connect-onedrive` answers 501;
the Cabinet hides the drive card only when the adapter itself is absent (To Do keys unset).

## Drive Indexer Service

`alek-bot-indexer-dev` runs the per-user `drive_sync` tick of the drive index
([`../10_rfcs/USER_DRIVE_INDEXING_RFC.md`](../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §12.5;
[decision](../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md)). It is `indexer_main.py` on the
same image: a `/worker` that serves `drive_sync` only (plus `/health`), gen2, 2 GiB, concurrency 1, timeout 1800 s,
min 0, max 1. **Ingress is internal** and the in-app OIDC check stays: a request from outside Google's network gets
404 before it reaches the app, and on Cloud Run the check fails closed when no accepted identity is configured
(`DRIVE_INVOKER_SA_EMAIL`, else `SERVICE_ACCOUNT_EMAIL`, plus the optional `OIDC_TRANSITION_SA_EMAILS` list; RFC Δ21). The token audience is pinned to the indexer's own URL (`{url}` or `{url}/worker`), so a manual task sent to a
run.app URL form other than the one in `INDEXER_URL_DEV` gets 401. The main service (1 GiB) never indexes. The
scheduler job `alek-bot-dev-start-drive-sync` sends it `start_drive_sync` every 10 minutes (minutes 3, 13, …), and
it fans out one `drive_sync` per user; it also nudges after bot writes, and the Cabinet toggle enqueues one. All of
them go through the dedicated queue `drive-sync-dev` (`max-concurrent-dispatches=1`) with a 1800 s dispatch
deadline. Queue, scheduler job and the build identity's queue permission
(`roles/cloudtasks.queueAdmin`, granted on the queue to the identity the build runs as): [SCHEDULERS.md](SCHEDULERS.md). `cloudbuild-dev.yaml` deploys the
queue and the indexer after the main service, so a failing drive step never blocks a fix of the bot; on the second
pass of the first deploy a task may meet the indexer's 503 for a minute or two and is retried by the queue.

### Indexer URL — two-pass first deploy

Both services read the indexer's URL from `DRIVE_INDEXER_URL` (an optional knob, never in `load_settings()`): the
main service targets the drive queue at it, and the indexer enqueues its own continuations there and pins its token
audience to it. **While it is empty, no drive task queue is built, and indexing never runs:**
- the main service logs `Drive sync queue: disabled` at INFO on startup;
- the main service answers the `start_drive_sync` job with `200 {"status": "disabled"}` and logs at INFO;
- the nudges are skipped without a log line;
- the Cabinet toggle saves the flag, enqueues nothing and logs a WARNING;
- the indexer logs `DRIVE_INDEXER_URL is not set` at WARNING on startup, and its `/worker` answers 503 to a
  `drive_sync`, logging an ERROR. No such request is expected: nothing enqueues to it on this pass.

**Deploy only from a checkout that has the real `.env`.** `make deploy` reads it through `include .env` (only when the
file exists) and passes `SERVICE_URL_DEV`, the relay URL and `INDEXER_URL_DEV` as Cloud Build substitutions.
Without it the build ships empty values into the live environment and breaks OAuth and voice. A second working copy
has no `.env` of its own: link the main checkout's file first (`ln -s ../alekbot-core/.env .env`). Before every
deploy, check what will be sent with `make -n deploy | grep -o -- '--substitutions=[^ ]*'`. Every value must be
non-empty, except `_INDEXER_URL` on the first pass below. `make deploy` itself refuses to start when
`SERVICE_URL_DEV` is empty, and when `INDEXER_URL_DEV` is empty unless `DRIVE_FIRST_PASS=1` is set: an empty indexer
URL silently disables indexing in the live environment, with no alert.

0. **Once:** the identity Cloud Build runs as needs `roles/cloudtasks.queueAdmin` on the drive queue
   ([SCHEDULERS.md](SCHEDULERS.md) § Start Drive Sync).
   **Indexes first.** Only the drive indexes are new: eleven composites and two `card_vector` vector indexes on
   `development_drive_index_records` (RFC Δ22). **In dev they already exist and are `READY` (created 2026-10-12, all
   13)**, so this step is a check there; on a fresh database, create them. `make deploy-indexes` issues every entry of
   `config/firestore.indexes.json` (an existing one answers "already exists"; read the output, not only the exit
   code) and skips vector entries; the two `card_vector` indexes come from
   `scripts/infrastructure/create_drive_vector_indexes.sh` (`COLLECTION_PREFIX` defaults to `development_`). Both
   need the project id passed explicitly: `.env` has `GOOGLE_CLOUD_PROJECT`, not `PROJECT_ID`, so run
   `make deploy-indexes PROJECT_ID=<PROJECT_ID>` (or `python scripts/infrastructure/deploy_firestore_indexes.py
   config/firestore.indexes.json <PROJECT_ID>`) and `PROJECT_ID=<PROJECT_ID> scripts/infrastructure/create_drive_vector_indexes.sh`.
   Wait until every new index is `READY`: the tick's own `due_records` query needs them, not only search. This prints
   the ones still building:
   ```bash
   gcloud firestore indexes composite list --database=us-production --project=<PROJECT_ID> \
     --format='table(name.basename(), state)' | grep -v READY
   ```
   Then run the read-only smoke check, which runs every repository query once and names any missing index:
   `venv/bin/python scripts/infrastructure/drive_index_smoke.py <DEV_USER_ID>`.
1. **Deploy once.** `DRIVE_FIRST_PASS=1 make deploy` creates the queue, `alek-bot-indexer-dev` and the scheduler
   job. Both services ship with an empty `DRIVE_INDEXER_URL` on this pass (expected; the log lines are listed
   above). The flag is for this pass only; never set it in `.env`.
2. **Read the indexer's URL back.** It is stable once the service exists:
   ```bash
   gcloud run services describe alek-bot-indexer-dev --region=us-central1 --format='value(status.url)'
   ```
3. **Put it in `.env`** as `INDEXER_URL_DEV=<that https:// URL>` (no trailing slash). The `deploy` target passes it
   to Cloud Build as `_INDEXER_URL`, and `cloudbuild-dev.yaml` sets `DRIVE_INDEXER_URL` on both services. It is a key
   of its own, never a reuse of `SERVICE_URL_DEV` or another dual-purpose `.env` key (CLAUDE.md, "Deploy-substitution
   trap"; the new substitution is declared in RFC Δ21). Manual tasks must use this same URL (see the 401 above).
4. **`make deploy` again**, then check:
   ```bash
   gcloud run services describe alek-bot-indexer-dev --region=us-central1 --format=yaml | grep -A1 DRIVE_INDEXER_URL
   gcloud run services describe alek-bot-dev --region=us-central1 --format=yaml | grep -A1 DRIVE_INDEXER_URL
   gcloud tasks queues describe drive-sync-dev --location=us-central1 \
     --format='value(rateLimits.maxConcurrentDispatches)'            # 1
   gcloud scheduler jobs describe alek-bot-dev-start-drive-sync --location=us-central1 \
     --format='value(schedule,state)'                                 # 3-59/10 * * * *  ENABLED
   curl -s -o /dev/null -w '%{http_code}\n' -X POST "<indexer URL>/worker" -d '{}'   # 404 (ingress), never 2xx/5xx
   ```
5. **Write round trip under a synthetic user.** The unit suite runs the Firestore repository against a stubbed SDK
   only, so before indexing is turned on for anyone, exercise the real writes once under a user id that is not a real
   user (e.g. `smoke-drive-<date>`): `save_sync_state` and `upsert_records` with a few records (one with a card
   vector), read them back with `get_records`, `due_records`, `find_by_name_keys` and `find_nearest_cards`, compare
   with the same calls on `InMemoryDriveIndexRepository`, then purge (`delete_user_records` until it returns 0, then
   `delete_sync_state`) and check that nothing is left under that user id. Use a throwaway script outside the repo
   (or under `scripts/memory/`); it must never touch a real user's documents.
6. **Alert policies cover the indexer.** The policies in [ALERTING.md](ALERTING.md) were written for `alek-bot-dev`.
   Check each policy's filter: any filter on `resource.labels.service_name="alek-bot-dev"` misses
   `alek-bot-indexer-dev`, so its tick errors (`severity>=ERROR`) and `/worker` 5xx would never page. Widen those
   filters to the indexer too (or add its own policy) and note it in ALERTING.md.

### Slice 2+4 — content, scans and images (owner steps)

USER_DRIVE_INDEXING_RFC §24 adds model calls and chunk storage to the indexer. The steps run **in this order**; each
says why it cannot move later. Background for every step: the building block
[`docs/05_building_blocks/user_drive/README.md`](../05_building_blocks/user_drive/README.md) §7.2.

1. **The first image build.** The Dockerfile now creates the system user `driveparser`, sets
   `DRIVE_PARSER_USER=driveparser`, and fails the build on any world-writable file under `/app` (`node_modules`
   aside) or in site-packages. Neither step was built locally. Build this branch's image and push it **without
   deploying** (`make deploy` builds and deploys in one Cloud Build, and the parser must not ship before S1; there is
   no image-only make target): the build must show `useradd` succeeding in the base image and the check passing, and
   step 2's S1 runs from that image. The image is shared, so a failure here
   blocks the main service's build too. On the indexer, a missing parser user fails the start
   (`parser user 'driveparser' does not exist in this container`, ERROR) rather than parsing unsandboxed; its start
   log names the user: `[DriveExtract] parser children run as driveparser (uid …, gid …)`.
2. **Spikes, recorded in the RFC (§18).** S1 before the parser ships: run `scripts/infrastructure/drive_extract_s1.py`
   as a one-off Cloud Run Job from step 1's image (gen2, the indexer's memory), so `DRIVE_PARSER_USER` is set and
   the child runs as the `driveparser` user, as deployed. It measures the Phase 0 bomb set under the child limits,
   the peak RSS with a 45 MB download, cold start, and for the long PDFs a **reader-shaped run**: the parent holds
   `MODEL_IN_FLIGHT` windows' bytes, base64 and request bodies while the child cuts the next window (simulated, no API
   call), so the cgroup peak includes the parent's side of a read. S0g (Graph custom renditions `c{w}x{h}` for App
   Folder items, legible receipt text, HEIC) before the reader reads images; S3 (`find_nearest` cost and latency at
   10k and 50k chunks under a `user_id` filter) before the slice ships.
3. **Model keys on the indexer.** The reader and triage agents call Claude (default) or OpenAI, so the indexer mounts
   `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from Secret Manager (`--set-secrets` in `cloudbuild-dev.yaml`, the same
   secret names the main service mounts). They are never Cloud Build substitutions. The runtime service account
   already reads them for the main service; check it once with
   `gcloud secrets get-iam-policy ANTHROPIC_API_KEY --project=<PROJECT_ID>`.
4. **The drive prompts, uploaded by the owner** (`docs/how_to/NEW_AGENT_PLAYBOOK.md`; never from an agent session).
   Twelve files in `firestore_utils/uploads/` (gitignored; the copies are in the main checkout):
   `COGNITIVE_PROCESS_DRIVE_READER.groovy` / `.json`, `COGNITIVE_PROCESS_DRIVE_TRIAGE.groovy` / `.json`,
   `OUTPUT_FORMAT_DRIVE_READER.groovy` / `.json`, `OUTPUT_FORMAT_DRIVE_TRIAGE.groovy` / `.json` (the tokens),
   `drive_reader.json`, `drive_triage.json` (the blueprints) and `drive_reader_agent_v1.json`,
   `drive_triage_agent_v1.json` (the profiles). Without them every model call fails as a model outage (logged ERROR,
   never charged to a file): parsed documents are still indexed, scans and images wait.
5. **Indexes, then wait for `READY`.** The chunk collection `development_drive_index_chunks` needs three composites
   (the term stream with and without `kind`, generation collection), the single-field exemptions for `text` and
   `vector`, and two vector indexes on `vector` under `user_id` and `user_id + kind`; the records collection gains ten
   composites (eight for the `unread` / `not_read_yet` counts, four of them with `triage_verdict` and two with
   `card_capped`; one for the readable-chunk total; one for chunk-cap release). Composites and exemptions:
   `make deploy-indexes PROJECT_ID=<PROJECT_ID>`; vectors:
   `PROJECT_ID=<PROJECT_ID> scripts/infrastructure/create_drive_vector_indexes.sh`. **Wait until every one is
   `READY`** (the `gcloud firestore indexes composite list … | grep -v READY` check above), then run the smoke check,
   which also runs the chunk queries and names any missing index. Why it must be first: a query whose composite is
   missing or still building fails with `FailedPrecondition`, which the indexer treats as a store outage
   (`[DriveIndex] index store … failed, an index is missing or building`, ERROR, with the provider's message naming
   the index). The tick ends there and counts nothing against any file, so nothing is lost — but no file's content
   is indexed until the indexes are `READY`.
6. **Check the reader's resolved provider, then the live model check — before the deploy.** The drive agents take the user's
   `agent_providers["drive_reader"]` / `["drive_triage"]`, then the user's global `provider_preference` (when it is
   `claude` or `openai`), then the default Claude. A user whose global preference is OpenAI reads with GPT without
   naming the drive agents. Read the owner's `config` on the user document (`development_domain_users_v2`; the
   Cabinet does not show these fields) and run the live model check on the provider that will actually read, or on
   both: `venv/bin/python -m scripts.validation.drive_model_check read <folder> --providers claude,openai`. It runs
   locally with the uploaded prompts (step 4) and settles the model pick, the 180 s window timeout and the thumbnail
   size before they ship. Switching the provider later: building block §4.16.
7. **Live check, with the drive queue paused — before the deploy** (§22 step 2, §24.12). It runs the real tick,
   parser, reader and triage locally against live Firestore, so it needs steps 4 and 5, not the new revision. The main service's fan-out enqueues a tick for
   every user with a sync state, the check's synthetic user included, so a live tick would race the check. Pause the
   queue, run the check, resume it:
   `gcloud tasks queues pause drive-sync-dev --location=us-central1 --project=<PROJECT_ID>`, then
   `venv/bin/python -m scripts.validation.drive_eval.live_check --folder scripts/memory/drive_live --user
   zz-synthetic-<yyyymmdd> --query …`, then `gcloud tasks queues resume drive-sync-dev --location=us-central1
   --project=<PROJECT_ID>`. The script refuses to start unless the queue reads PAUSED, never pauses or resumes it
   itself, and prints the resume command when it ends. Ticks queued meanwhile run after the resume.
8. **Smart's drive guidance, before the deploy.** The `drive_specialist` block of `PROTOCOL_AGENT_SELECTION`
   (`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`): back up the prompt layer
   (`firestore_utils/backup_prompt_layer.py` into `scripts/memory/`), check the mirror for drift
   (`python firestore_utils/snapshot_pull.py --check`), dry-run the upload, then
   `python firestore_utils/snapshot_upload.py --apply prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`
   (owner only, interactive; [prompts_snapshot/README.md](../../prompts_snapshot/README.md)). Upload it **before**
   deploying: the new `render_results` no longer carries the guidance the token now holds, and the prompt assembly
   caches a template for 24 h per instance, so the new revision's fresh instances must find the new token. (The old
   revision with the new token only repeats guidance it already rendered: harmless for the minutes in between.)
9. **Deploy.** `make deploy` (the Indexer URL steps above apply as usual). The upgrade pass then re-plans the Slice 1
   records in class 3; while it has not reached them all, search says "Documents are still being read".

**Cost.** A page read by the model costs about $0.0006 on Haiku 5.5 or GPT-6 Luna; the monthly page budget bounds
**the reader** at about $3 per user. That is not the whole bill: triage is bounded by 20 000 images per user per month
(its price is measured by the live model check), embeddings have no monthly cap (bounded by 25 000 cards and 50 000
chunks per user, and not billed to the user), and Firestore grows with the chunk count. Every path and its bound:
the building block's §4.13 and RFC §24.9. Model calls are billed to the user's account and appear in
`billing_daily_summary`; the tick summary line carries `model_pages`, `model_calls`, `triage_images` and
`model_cost`.

**Rollback: there is none to the Slice 1 revision.** Redeploying it over Slice 2+4 data fails: its `Reason` has no
`photograph`, so a tick that reads such a record crashes, and it never deletes chunk documents. The levers are
forward changes, each a code change plus a deploy:
- stop all content work and collect every chunk: empty `ADMITTED_KINDS` and `TRIAGED_KINDS` in
  `src/domain/drive_index_policy.py` and bump `POLICY_VERSION` (in `src/domain/drive_index.py`); the upgrade pass re-classifies every record as
  `kind_not_admitted` and collects the readable chunks;
- stop model spend only: remove `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the indexer's secrets; every model call
  is then a model outage (never charged to a file), and parsing, cards and search go on. The Gemini Flash fallback
  does not take over: a primary without a key fails the context build before any failover (`GEMINI_API_KEY` stays,
  embeddings need it); to turn the fallback off, set `"fallback": None` in the two drive entries of
  `AgentProviderStrategy.STRATEGIES`;
- one user: turn their indexing off in the Cabinet; the purge removes hint documents (Search v2), then chunks,
  records and sync state.

**Rebuild one user's index.** `venv/bin/python scripts/infrastructure/drive_index_rebuild.py --user <USER_ID>` is a
dry run; add `--apply` to mark the sync state `purging` under the user's lease (refused while a tick runs: retry in a
few minutes). The next tick purges the index and re-onboards from epoch 1 (USER_DRIVE_INDEXING_RFC §12.2). Under
Search v2 this is also the only migration (D30); `--all-users` visits every sync state and `--hints-only` deletes
hint documents only (the rollback's step, below).

**Logs.** The indexer has no Logfire telemetry: Cloud Logging only. A tick that ends `done` or `continued` logs one
`[DriveIndex] tick user=… end=… {…} duration=…` line on the indexer, not on the main service, so `make fetch-logs`
does not show it: `make fetch-logs-indexer K=300` writes `alek_debug_indexer.log` (`make logs-indexer` prints
instead). A throttled tick logs a WARNING and a failed one an ERROR instead of that line; a purge logs
`index purged`. An item whose observation fails inside a page is skipped with one ERROR line. That is expected
behaviour (§8.2), not an incident, unless the same item repeats on every tick. From Slice 2+4 each index run also logs one INFO
`[DriveIndex] run user=… item=<id> outcome=… state=… reason=… extractor=… attempts=… pages=<sent>/<total> refused=…
truncated=…` line, and the reader one `[DriveReader] user=… item=<id> …` line per file; "why was file X not indexed"
is answered from the record and these lines (building block §7.1).

**Until the branch is merged**, a `make deploy` from another branch removes `start_drive_sync` from the main service
while the scheduler job keeps firing. Pause the job first:
`gcloud scheduler jobs pause alek-bot-dev-start-drive-sync --location=us-central1`.
After the next deploy from this branch, resume it:
`gcloud scheduler jobs resume alek-bot-dev-start-drive-sync --location=us-central1`. A deploy does not resume a
paused job.

### Search v2 — rollout and rollback (D30)

USER_DRIVE_INDEXING_RFC §25.8, as amended by D30 (no migration of v1 indexes: every user is purged and re-indexed
from scratch) and by the cross-cutting operations review. The steps run **in this order**; each says why. Every
command reads its values from `.env`; the resource names are the ones this README already uses. One shell:

```bash
set -a; source .env; set +a          # GOOGLE_CLOUD_PROJECT, FIRESTORE_DATABASE, DEV_USER_ID
REGION=us-central1 QUEUE=drive-sync-dev SYNC_JOB=alek-bot-dev-start-drive-sync INDEXER=alek-bot-indexer-dev
PY=venv/bin/python
```

Other users' ids are never written here: `$PY scripts/infrastructure/drive_index_rebuild.py --all-users` (a dry run)
lists every user with a sync state.

**Gates, before anything runs** (§25.7, §25.4.3):
- the search eval meets `bars_v2.json`: `$PY -m scripts.validation.drive_eval.search_v2_eval` (offline, exit 0), and
  the owner's run with real vectors, `… search_v2_eval --embeddings gemini`;
- G3 has no hard failure: `$PY -m scripts.validation.drive_eval.smart_dry_run --repeats 3` (owner-run, paid);
- the caption-shift gate (§25.4.3: no description on the wrong image over ≥ 5 runs of the eval photo fixture, and
  the owner's look) and the live model check of the new agents
  (`$PY -m scripts.validation.drive_model_check profile|passages|triage …`).

**Rollout**
1. **Back up the prompt layer** (owner): `$PY firestore_utils/backup_prompt_layer.py` (into `scripts/memory/`). The
   rollback restores the triage v1 tokens and `PROTOCOL_AGENT_SELECTION` from it; without it there is no way back.
2. **Indexes, all `READY`, before any deploy.** `make deploy-indexes PROJECT_ID=$GOOGLE_CLOUD_PROJECT` creates the
   record, chunk and hint composites and the single-field exemptions; it does **not** create vector indexes, so also
   run `PROJECT_ID=$GOOGLE_CLOUD_PROJECT scripts/infrastructure/create_drive_vector_indexes.sh` (the two
   `hint_vector` indexes). Wait until this prints nothing:
   ```bash
   gcloud firestore indexes composite list --database=$FIRESTORE_DATABASE --project=$GOOGLE_CLOUD_PROJECT \
     --format='value(name,state)' | grep -v READY
   ```
   Then turn on the TTL of the monthly usage documents (the `{user}:initial` document has no `expires_at`):
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=development_drive_model_usage \
     --enable-ttl --database=$FIRESTORE_DATABASE --project=$GOOGLE_CLOUD_PROJECT
   ```
   and run every v2 read once: `$PY scripts/infrastructure/drive_index_smoke.py $DEV_USER_ID`. Why first: every v2
   search, a gated one included, runs the about, word and hint composites and `count_by_job_state`; before `READY`
   each is `FailedPrecondition`, so every search fails.
3. **Pause the drive queue and the fan-out job, then wait for the running tick.**
   ```bash
   gcloud tasks queues pause $QUEUE --location=$REGION --project=$GOOGLE_CLOUD_PROJECT
   gcloud scheduler jobs pause $SYNC_JOB --location=$REGION --project=$GOOGLE_CLOUD_PROJECT
   ```
   A v1 tick already running keeps going on the old revision for up to 30 minutes and holds the user's lease: wait
   until `make logs-indexer` shows its `[DriveIndex] tick … end=` line, or 30 minutes.
4. **Upload the indexer prompts while paused** (owner; `docs/how_to/NEW_AGENT_PLAYBOOK.md`): the profiler, the
   passages agent and triage v2 (tokens, blueprints, profiles from `firestore_utils/uploads/`). Why now: triage v2
   with the v1 tokens fails silently — the schema is enforced, the reply parses, and images get stamped
   `TRIAGE_VERSION` 2 with thin descriptions until the next bump.
5. **Deploy main and the indexer:** `make deploy`. Then check that both are still paused (a deploy never resumes
   them):
   ```bash
   gcloud tasks queues describe $QUEUE --location=$REGION --project=$GOOGLE_CLOUD_PROJECT --format='value(state)'
   gcloud scheduler jobs describe $SYNC_JOB --location=$REGION --project=$GOOGLE_CLOUD_PROJECT --format='value(state)'
   ```
   Upload Smart's protocol token (`PROTOCOL_AGENT_SELECTION`, the `tiers` line) right after the main deploy
   (§ Slice 2+4 step 8 for the commands). From here every v1 index is **gated** (D30): its ticks do no work and its
   search answers from no v2 record, until that user's rebuild — rebuild promptly.
6. **Resume the queue and the job BEFORE the rebuild.** The purge is done by a tick: with the queue paused it never
   runs.
   ```bash
   gcloud tasks queues resume $QUEUE --location=$REGION --project=$GOOGLE_CLOUD_PROJECT
   gcloud scheduler jobs resume $SYNC_JOB --location=$REGION --project=$GOOGLE_CLOUD_PROJECT
   ```
7. **Rebuild the owner first.** `$PY scripts/infrastructure/drive_index_rebuild.py --user $DEV_USER_ID` (the dry run
   says "a v1 index awaiting its Search v2 rebuild (D30)"), then the same with `--apply`. The next tick purges hint
   documents, chunks, records and the sync state (the usage and initial-build documents stay), and onboarding starts
   under v2, the initial build spent newest-first. Watch `make logs-indexer`: `index purged`, the onboarding ticks,
   `rebuild progress user=… described=n/m`, `initial build closed`; and the Cabinet's initial-build line.
   **Expect** the account's advisory $5 daily-cost alert on most days of a rebuild (about $3–7 a day for 1 000–2 400
   files, plus scans); embeddings are the owner's cost and are not billed to the user. The stall alert fires after
   24 h without progress while jobs are ready and budget is left.
8. **The owner's live check**, then every other user, one at a time: `… drive_index_rebuild.py --user <id> --apply`
   with the ids from the `--all-users` dry run.
9. **After acceptance**, drop the two `content_terms` chunk composites (`gcloud firestore indexes composite delete
   <index name> --database=$FIRESTORE_DATABASE --project=$GOOGLE_CLOUD_PROJECT`, names from the list above). The
   rollback then needs them recreated (step 5 below).

**Rollback** (to the tag `drive-search-v1`). The order keeps v1 from ever serving a v2 record:
1. Pause the queue and the job (rollout step 3), and wait for the running tick.
2. **Mark every user `purging` while v2 is still deployed:**
   `$PY scripts/infrastructure/drive_index_rebuild.py --all-users --apply`. v2's search then answers "building";
   nothing is purged yet (the queue is paused).
3. **Delete every hint document:** `$PY scripts/infrastructure/drive_index_rebuild.py --all-users --hints-only`
   (dry run: counts), then `--apply`. The tag's purge does not know `drive_index_hints`, and those documents hold
   quotes of the users' passages: after the tag is deployed nothing would ever delete them.
4. **Restore the v1 prompts** from the step-1 backup (owner): the triage v1 tokens and `PROTOCOL_AGENT_SELECTION`.
5. **If the `content_terms` composites were dropped**, from a checkout of the tag: `make deploy-indexes
   PROJECT_ID=$GOOGLE_CLOUD_PROJECT`, and wait for `READY`.
6. **Deploy the tag**, main and indexer, from a checkout of `drive-search-v1` with the same `.env`
   (`git worktree add ../alekbot-core-v1 drive-search-v1`, then `make deploy` there).
7. **Resume the queue and the job.** The tag's first tick finds the state `purging`, purges and rebuilds under v1.
   The usage documents survive; the tag does not read them.
8. **Before any later roll-forward**, reset each user's initial build, which the first v2 build spent or closed and
   no purge renews: `$PY scripts/infrastructure/drive_initial_build_reset.py --user <id>` (dry run: its state), then
   `--apply` (default allowance $40, never under $20).
   (The Card accuracy rollout below does **not** use the reset: owner rule 2026-10-19.)

**The emergency switch does not survive a deploy.** `DRIVE_UPGRADE_PASS=off` stops every upgrade pass:
`gcloud run services update $INDEXER --update-env-vars DRIVE_UPGRADE_PASS=off --region=$REGION
--project=$GOOGLE_CLOUD_PROJECT`. The next `make deploy` deploys the indexer with `--set-env-vars`, which replaces
every variable, so the switch silently turns back on: set it again after each deploy while it is needed.

### Card accuracy (§26) — rollout and rollback

USER_DRIVE_INDEXING_RFC §26.9 (card accuracy, capture, files the bot cannot open, photo location and scene labels;
branch `feat/drive-card-accuracy`, slices S1–S6). It reuses the shell variables and the pause/resume commands of
§ Search v2 above. `MIGRATION_GATE_KEY` moves to `accuracy`, so every existing index is **gated** from the deploy until
its owner-run rebuild (no tick work, search says "being rebuilt", one ops alert). **No initial-build reset** (owner
rule, 2026-10-19): the rebuild runs under the monthly caps (reader $4, triage $3, profiler $4, passages $20).
What that means: profiles ≈ 165 a day, reader ≈ 6 600 pages a month, passages ≈ 50 000 chunks a month; work beyond a
cap waits for the next UTC month. Cost of a typical drive: RFC §26.7.

**Gates, before anything runs:** `make check` green; `$PY -m scripts.validation.drive_eval.search_v2_eval` meets
`bars_v2.json`; G3 (`smart_dry_run --repeats 3`, owner-run, paid) has no hard failure — it now includes the card
accuracy cases (`must_open`, `must_not_open`).

**Rollout**
1. **Back up the prompt layer** (owner): `$PY firestore_utils/backup_prompt_layer.py`. The rollback restores the six
   tokens below from it.
2. **Indexes `READY`:** `make deploy-indexes PROJECT_ID=$GOOGLE_CLOUD_PROJECT` (the new composite `user_id +
   inaccessible_since`), the same `READY` check as Search v2 step 2, then `$PY scripts/infrastructure/drive_index_smoke.py
   $DEV_USER_ID` (it now runs `inaccessible_records`).
3. **Pause the queue and the fan-out job, wait for the running tick** (commands as Search v2 step 3).
4. **Upload the six changed tokens inside the paused window** (owner; `docs/how_to/NEW_AGENT_PLAYBOOK.md`, from
   `firestore_utils/uploads/`): `OUTPUT_FORMAT_` and `COGNITIVE_PROCESS_` for the profiler, the reader and triage
   (`…_DRIVE_PROFILER`, `…_DRIVE_READER`, `…_DRIVE_TRIAGE`; repo copies in `prompts_snapshot/tokens/system/`). Why in
   the window: a tick running the new code against old tokens (or the reverse) stamps records with a schema the other
   side cannot read.
5. **Deploy main and the indexer:** `make deploy`. The skill, the locales and the manifest text ship with the code.
   Check both are still paused.
6. **Verify both services serve the new image** before resuming and rebuilding:
   ```bash
   gcloud run services describe $INDEXER --region=$REGION --project=$GOOGLE_CLOUD_PROJECT --format='value(status.latestReadyRevisionName,spec.template.spec.containers[0].image)'
   ```
   and the same for the main service (its name is in `.env`). A partial deploy followed by a rebuild means a second rebuild.
7. **Resume the queue and the job** (the purge is a tick).
8. **Rebuild per user, the owner first:** `$PY scripts/infrastructure/drive_index_rebuild.py --user $DEV_USER_ID`
   (dry run), then `--apply`. The script **skips a state that already carries the gate key** (`accuracy`), so a re-run
   never purges a rebuilt user; `--force` overrides. Then every other user from the `--all-users` dry run, one at a
   time. Watch `make logs-indexer`: `index purged`, onboarding, `rebuild progress user=… described=n/m`.
9. **Validate on the owner's real scans** (RFC §26.7, D38) — on scans the reader has already read (its monthly cap may
   not have reached all of them yet) — and run the revision script, `$PY -m scripts.validation.drive_card_review --user
   $DEV_USER_ID` (output to `scripts/memory/`; `--below 90`, `--weights <file.json>` to try candidate weights). Weights
   and the 90 % bar change only from that report. After a few weeks of real queries, `$PY -m
   scripts.validation.drive_query_vocabulary --user $DEV_USER_ID --since <deploy date>` measures vocabulary misses
   before any profiler prompt change (Δ66).

**Rollback** (the previous main tip). Records parse under the old code (§26 adds fields and no enum value, RFC Δ47),
but the index must be rebuilt, not upgraded in place:
1. Pause the queue and the job; wait for the running tick.
2. Mark every user `purging` while the new code is still deployed: `$PY scripts/infrastructure/drive_index_rebuild.py
   --all-users --apply --force`.
3. **Restore the six-token backup** (owner).
4. Deploy the previous tip (main and indexer).
5. Resume the queue and the job. **The purge is mandatory**, and the re-index that follows is a **second one**, under
   monthly caps the first already spent: expect it to be slower.

### Transcripts of recordings (§27)

USER_DRIVE_INDEXING_RFC §27 (slice T1, live since 2026-10-21 for the owner; the T2 mirror folder is built and not deployed, see "T2 mirror folder" below; T3 summary is not built); the owner's
map is `docs/05_building_blocks/user_drive/README.md` §4.23. An **ordinary upgrade on top of §26**: no rebuild, no migration gate, no
initial-build reset (Δ126). It reuses the shell variables and the pause/resume commands of § Search v2 above. Two switches keep it
off until the owner turns it on: the indexer's kill switch and the user's own flag. Nothing here is a secret; the key names only.

**Keys (deploy-only, in `.env`, never the local `DRIVE_TRANSCRIBE_ENABLED` / `DRIVE_TRANSCRIBE_USD_CAP`: the deploy-substitution trap).**
- `DRIVE_TRANSCRIBE_ENABLED_DEV` — `on` turns the kill switch on, `off` turns it off (no other value is accepted, T-R34). **Required**: `make deploy` refuses unless it is exactly `on` or `off` (T-R34), so a checkout without the key cannot silently deploy it off. It also rides to the main service, where the Cabinet shows the disclosure only if it is on. The Makefile
  passes it as `_DRIVE_TRANSCRIBE_ENABLED`, and `cloudbuild-dev.yaml` sets `DRIVE_TRANSCRIBE_ENABLED` on the indexer. It is a Cloud Build substitution
  and not a by-hand variable because `--set-env-vars` replaces every variable on each deploy: a value set with `gcloud run services update` is gone at
  the next deploy (the same reason as `DRIVE_UPGRADE_PASS`). A manual `gcloud builds submit` without the Makefile deploys `off`.
- `DRIVE_TRANSCRIBE_USD_CAP_DEV` — optional number of dollars, e.g. `10`. It can only **lower** the $20 monthly cap (a larger or unparsable
  value is the default). Empty = $20. Set on the indexer and on the main service (the Cabinet shows the effective cap). **Set it low for
  the first run**: turning the flag on starts the whole backlog, newest recordings first, and this cap is the only lever between off and $20.
- `GEMINI_API_KEY` already exists on the indexer (embeddings, fallback). With the switch on and no key the stage is not built and an ERROR is
  logged.

**Rollout**
1. **Gates:** `make check` green on the branch.
2. **Back up the prompt layer** (owner): `$PY firestore_utils/backup_prompt_layer.py`.
3. **Indexes `READY`:** `make deploy-indexes PROJECT_ID=$GOOGLE_CLOUD_PROJECT` creates the new composites (records: `user_id +
   transcript_state + effective_day + parent_id + item_id`, `user_id + transcript_wake_at + item_id`; the job, batch and store collections), the **two collection-group indexes**
   on the window and part subcollections (`user_id + index`; the purge's orphan sweep needs them) and the single-field exemptions for the large
   `segments` and `summary` fields. Same `READY` check as Search v2 step 2, then `$PY scripts/infrastructure/drive_index_smoke.py $DEV_USER_ID`
   (it now runs every transcript read, including the two collection-group reads). Without the collection-group indexes the sweep is logged ERROR and
   skipped; it never fails or holds a purge.
4. **Upload the four prompt documents** (the owner's act; `docs/how_to/NEW_AGENT_PLAYBOOK.md`; by default never from an agent session; the first run's exception is below), after the backup, from
   `firestore_utils/uploads/`: tokens `COGNITIVE_PROCESS_DRIVE_TRANSCRIBER` and `OUTPUT_FORMAT_DRIVE_TRANSCRIBER`, the blueprint
   `drive_transcriber_agent_v1` and the profile `drive_transcriber` (repo copies of the sources in `prompts_snapshot/`). Without them the stage cannot
   submit: a missing prompt is a model outage for this class (no fallback prompt), so nothing is lost, and the same four are all that T1 needs
   (the summary agent's are T3). The prompt assembly caches a template for 24 h per instance.
5. **Pause the queue and the fan-out job**, wait for the running tick (commands as Search v2 step 3).
6. **Deploy main and the indexer together with the switch OFF:** `make deploy` with `DRIVE_TRANSCRIBE_ENABLED_DEV=off` (since T-R34 `make deploy`
   refuses unless that key is explicitly `on` or `off`, and echoes the effective switch and cap before it submits). The policy bump
   (`POLICY_VERSION` 3) makes the upgrade pass rewrite **every** record of every user (113 of 113 for the owner), not only the media; the
   media are re-classified to `pending`, metadata only, and the rest is a no-op rewrite; nobody's flag is on, so no
   recording counts as "not read yet" and nothing is sent anywhere. `ffmpeg` and the unprivileged parser user are in the image already.
7. **Verify both services serve the new image** (command as § Card accuracy step 6), then **resume** the queue and the job.
8. **Estimate the backlog** before enabling (RFC §27.15 step 5): sum the owner's recording durations (read-only; output only under
   `scripts/memory/`). At the held price a hour books about $0.16, so $20 buys about 125 hours a month until 2027.
9. **To try it:** set `DRIVE_TRANSCRIBE_ENABLED_DEV=on` and `DRIVE_TRANSCRIBE_USD_CAP_DEV=<low>` in `.env`, `make deploy` again (steps 5–7 around it),
   then set the owner's own flag: `scripts/infrastructure/drive_transcribe_flag.py` (usage `--user USER [--off] [--apply]`: dry run by default; `--apply` is the owner's act by default, `--off` turns the flag off). For the first run (2026-10-21) the owner explicitly delegated these steps to the assistant: the four prompt uploads (after a prompt-layer backup and a mirror check), the composite index deploy, the deploy with the switch off then on with a $10 cap, and setting the owner's per-user flag. Do
   not set it for any other user: the parser is not yet a separate zero-credential service (RFC OQ-T3).
10. **Watch:** `make logs-indexer` for the `[DriveIndex]` tick line (`windows_extracted`, `windows_submitted`, `windows_collected`,
    `transcripts`, `transcribe_usd`, `transcribe_reserved_usd`), the Cabinet's cost line, and the first transcript through a search by a
    sentence from the recording. Raise the cap only after reading the estimate and the first days.

**Rollback / levers, fastest first**
1. The user's flag off (the same flag script, `drive_transcribe_flag.py --user USER --off --apply`): his new work stops, his transcripts, store and index stay and keep answering.
2. Kill switch off: set `DRIVE_TRANSCRIBE_ENABLED_DEV=off`, redeploy. The stage is not built, so a switch-off (or any deploy with the key off)
   **freezes in-flight batches**: they stay at the provider and their reservations stay, and their cost is booked only when transcription is
   switched back on (or the user opts out of indexing and the purge books or releases them). Both `_DEV` keys
   (`DRIVE_TRANSCRIBE_ENABLED_DEV`, `DRIVE_TRANSCRIBE_USD_CAP_DEV`) must be in the `.env` of whichever checkout deploys.
3. The previous image: every new field is plain and no enum gained a value, so the old code parses every record and treats media as
   `kind_not_admitted` (it collects their chunks). A purge under old code leaves the transcript documents behind; the sweep script (RFC §27.15
   step 7) removes them.
4. Opt-out of drive indexing for one user: the purge removes jobs, batches and stored transcripts (a rebuild keeps the store).

**Also in this release:** a `google-genai` floor of 2.17.0 (`requirements.txt`; the batch surface was measured on it), the Cabinet's "transcribing
recordings" cost line, and the disclosure sentence (shown only for a user whose flag is on).

#### T2 mirror folder (§27.9; built on `feat/drive-transcripts-t2`, not deployed)

An **ordinary upgrade on top of T1**: the mirror calls no model, so there is no prompt upload, no rebuild, no migration gate and no new Cloud Build substitution. It
runs only for a user with both transcription switches on (today the owner). The owner's map is `docs/05_building_blocks/user_drive/README.md` §4.23 and §7.

**Pre-deploy gate (hard): the composite indexes must be `READY` before the T2 revision serves (RFC Δ230).** The indexer now wires the mirror ledger unconditionally
(one `roots` query per tick) and the purge reads the pinned keys. A missing or still-building index raises `FailedPrecondition`, which the adapter maps to
`DriveIndexStoreUnavailable`: a systemic store outage, so **every purge (opt-outs included) and the owner's ticks stall** until the index is ready. Do not deploy on
"it is building".
1. `make deploy-indexes PROJECT_ID=$GOOGLE_CLOUD_PROJECT` creates the composites declared in `config/firestore.indexes.json`: on the records `user_id + mirror_due +
   effective_day desc + item_id` and `user_id + mirror_retry_at + item_id`; on the ledger `user_id + drive_id + gone_pending + mark_ready_at`, `user_id + drive_id +
   mirror_parent_id`, `user_id + drive_id + kind + __name__`, `user_id + drive_id + state + checked_at`, the pins' four (`user_id + content_key + state`, `user_id +
   content_key + gone_pending`, `user_id + state + content_key`, `user_id + gone_pending + content_key`), `user_id + mirror_item_id`, and the deleted-files search's
   `user_id + state + archived_at desc`, `user_id + state + archive_keys` and `user_id + state + name_keys`; and the single-field exemption for `archive_keys`.
2. **Verify** (no project id in any tracked file; use your shell variable):
   ```bash
   gcloud firestore indexes composite list --database=us-production --project=$GOOGLE_CLOUD_PROJECT \
     --format="table(name, queryScope, state)" | grep -E "drive_transcript_mirror|drive_index_records"
   ```
   **Every printed row must be `READY`** (the grep keeps the ledger's indexes and the records' indexes, including the two new ones); any `CREATING` or `NEEDS_REPAIR` is a stop. Then
   `$PY scripts/infrastructure/drive_index_smoke.py $DEV_USER_ID`, which runs every new read and names a missing index.
3. Pause the drive queue and the fan-out job, wait for the running tick (commands as Search v2 step 3), then **deploy main and the indexer together**
   (`make deploy`, with the T1 switch key as already set). **Verify both serve the new image** (command as § Card accuracy step 6), then **resume**.
4. With the owner's flag on, the first tick creates `_Transcripts` and `_README.md`, the seed pass marks his existing transcripts due, and files appear at up to
   about 40 writes a tick (5 000 first-time files take about a day).
5. **Live check** (owner, on the phone): `_Transcripts` shows the right tree and its read-me; a plain search never returns a mirror file; a deleted test recording shows
   under `_Transcripts/Archive/<today>/` after one complete round and an hour; "search in deleted files" finds it and a plain search does not; restoring it from the recycle
   bin moves the file back; asking the bot to save a file into `_Transcripts` is refused. Then review the Cabinet texts in the four languages (Δ202).
6. **Watch:** `make fetch-logs-indexer K=300` and the `mirror={...}` part of the `[DriveIndex]` tick line, and `scripts/infrastructure/drive_mirror_status.py --user <user_id>`
   (read-only; the report goes to `scripts/memory/`).

**Rollback / levers, fastest first**
1. The user's flag off (`drive_transcribe_flag.py --user USER --off --apply`) or the kill switch off and a redeploy: the mirror step stops writing; the folder, the ledger
   and the root document stay, and the root stays opaque to the index.
2. **The previous image (a revision that predates T2; today that is the T1 image).** It does not know the root and would index the mirror's `.md` files as documents. So: pause the
   drive queue and the fan-out job; **move `_Transcripts` out of the App Folder** (it keeps its item id; do it in the OneDrive app, not through the bot); deploy the old image;
   resume. After the roll-forward: pause again, **move the folder back**, deploy, resume. A stray T2 tick in between only sets the root-loss mark, which the 24 h grace keeps from
   acting. This image keeps the store but not the pins: T1's daily orphan sweep deletes the pinned transcripts within 30 days (the archived files on the drive are untouched;
   their stored passages and the re-buy-free restore are lost). Named residual.
3. After a rollback to code that predates §27 only: `scripts/infrastructure/drive_transcript_sweep.py --user <user_id>` (dry run, then `--apply`, a human's act). It **keeps** the
   mirror's root documents, ledger and pinned store entries, because a roll-forward needs them. `--mirror` deletes all of them and is for retiring the mirror for good:
   **before `--mirror --apply` the owner must have deleted `_Transcripts` or moved it out of the App Folder, and no T2 revision may run for that user again until then** (once the
   root documents are gone a T2 revision indexes the folder as ordinary documents and, with the flag on, creates `_Transcripts (Alek)`). The script refuses `--mirror --apply`
   without `--after-rollback`, which is the owner's confirmation of that.
4. 5. An indexing opt-out deletes nothing on the drive (OQ-T10); a re-enable re-buys every active transcript, newest first, under the $20 cap (about $0.16 per audio hour).

## Sandbox render service

`alek-bot-sandbox-render-dev` runs `render.docx` (the Node script a model wrote for a DOCX) for the main service
([`../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4, §7 S1, §8;
[decision](../04_solution_strategy/decisions/sandbox_render_service.md)). It is `sandbox_main.py` in its own image
(`Dockerfile.sandbox`, target `render`; an explicit COPY allowlist, no `.env`, no key file): gen2, 2 GiB, 2 CPU,
concurrency 1, max 3, timeout 300 s, `--use-http2`, `--no-allow-unauthenticated`, an in-memory volume at `/work`
(size 256Mi, the default fixed in `scripts/infrastructure/cloudbuild_sandbox_render.sh`; a placeholder until S0-f, changed by editing that script, not through `.env`). One request per
instance (`SANDBOX_RECYCLE=always`). **Status: built, not accepted, not deployed.** The main service uses it only when
`SANDBOX_RENDER=sandbox`; the default is `inprocess`.

**`.env` keys** (each has its own Cloud Build substitution; never reuse a local key, see CLAUDE.md "Deploy-substitution trap"):

| `.env` key | Substitution | Meaning |
|---|---|---|
| `SANDBOX_RENDER_DEV` | `_SANDBOX_RENDER` | the switch on the **main** service: `inprocess` (default when empty) or `sandbox` |
| `SANDBOX_RENDER_URL_DEV` | `_SANDBOX_RENDER_URL` | the render service's own `https://` URL: the main service's target and the service's token audience (`SANDBOX_URL`) |
| `SANDBOX_RENDER_SA_DEV` | `_SANDBOX_RENDER_SA` | the render service's runtime identity; **empty skips every sandbox build step** |
| `SANDBOX_RENDER_CALLERS_DEV` | `_SANDBOX_RENDER_CALLERS` | emails admitted to `render.*` (`:`-separated; one email today; a comma is refused) |

**Owner steps, in order** (nothing here is run by an agent):

1. **Create the identity and grant it nothing.** Its name must start with `alek-sandbox-` (for example
   `alek-sandbox-render@<PROJECT_ID>.iam.gserviceaccount.com`): the deploy script refuses any other
   `SANDBOX_RENDER_SA_DEV` with exit 3, so a mis-set value cannot give the "holds nothing" service real IAM. Verify that
   `gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings[].members --filter="bindings.members:<sandbox-render-sa>"`
   prints nothing.
2. **Let the deployer act as it.** Grant `roles/iam.serviceAccountUser` on `<sandbox-render-sa>` (that account only) to the
   build's deployer identity (`alek-deployer`), so `gcloud run deploy --service-account=<sandbox-render-sa>` is allowed.
3. **Put the keys in `.env`:** `SANDBOX_RENDER_SA_DEV=<sandbox-render-sa>` and
   `SANDBOX_RENDER_CALLERS_DEV=<main-runtime-sa>` (the main service's own runtime identity, since the IAM migration).
   Leave `SANDBOX_RENDER_URL_DEV` empty and `SANDBOX_RENDER_DEV` unset for now. The callers must be set **before the
   first deploy** (below).
4. **Pass one: `make deploy`.** The sandbox steps run after the indexer steps (a failing step never blocks a fix of the
   bot). The service is created with `SANDBOX_URL` empty: it starts, serves **only `/health`**, and answers every op with
   `503 unconfigured`. It must still be given the callers, or Cloud Run refuses the revision (an empty caller set on Cloud Run
   is a start refusal by design).
5. **Read the URL back** (stable once the service exists):
   ```bash
   gcloud run services describe alek-bot-sandbox-render-dev --region=us-central1 --format='value(status.url)'
   ```
   and put it in `.env` as `SANDBOX_RENDER_URL_DEV=<https:// URL>`.
6. **Grant invoker to the main runtime only:** `roles/run.invoker` on the render service to `<main-runtime-sa>`. No other
   binding; never `allUsers` or `allAuthenticatedUsers`. Audit once and after each deploy: the project and service policies
   contain neither, and the render service is not in the public-invoker set (RFC §8 step 2).
7. **Pass two: `make deploy`.** The render service now knows its audience and serves; the main service now has the URL.
   The switch is still `inprocess`.
8. **Flip the switch** only after S0 and the DocGenerator end-to-end check in `sandbox` mode (the owner's go):
   `SANDBOX_RENDER_DEV=sandbox` in `.env`, then `make deploy`. With `sandbox` and an empty URL on Cloud Run the main
   service still starts (ERROR logged) and DOCX creation reports the sandbox unavailable; locally it is a startup error.

**Skip while the identity is empty.** With `SANDBOX_RENDER_SA_DEV` empty, `scripts/infrastructure/cloudbuild_sandbox_render.sh`
exits 0 in every mode (pull, build, push, deploy), so `make deploy` keeps working before step 1.

**Rollback.** Set `SANDBOX_RENDER_DEV=inprocess` (or empty) and `make deploy`: the main service builds `NodeDocxRunner`
again. The render service can stay up unused. There is no automatic fallback in either direction.

**Hard stop.** To cut a caller off at once, remove its `roles/run.invoker` binding on the render service. Do **not** empty
`SANDBOX_RENDER_CALLERS_DEV` to stop a misbehaving service: an empty caller list on Cloud Run refuses the new revision, so
the old revision keeps serving. To change who is admitted, redeploy the render service with the new list.

## Memory telemetry sink

Every process (main service, indexer, voice relay, research Job) writes `memory.boot`, `memory.sample`,
`memory.scope` and `memory.payload` records as structured log lines
(`docs/04_solution_strategy/decisions/memory_telemetry.md`). They reach BigQuery through a log sink; the
sink and the dataset are infrastructure outside the repo. One-time setup (owner step; `<PROJECT_ID>` and
the sink's writer identity come from `.env` / the command output, never from this file):

```bash
# 1. Dataset with 90-day partition expiry (separate from alek_observability_dev: that one has a 30-day TTL)
bq --project_id=<PROJECT_ID> mk --dataset --location=US \
   --default_partition_expiration=7776000 alek_memory_dev

# 2. Sink: only the memory.* records, into date-partitioned tables.
#    Use the substring operator `:`; the regex form `=~"^memory\\."` matches nothing in Cloud Logging filters.
gcloud logging sinks create alek-memory-telemetry \
  bigquery.googleapis.com/projects/<PROJECT_ID>/datasets/alek_memory_dev \
  --project=<PROJECT_ID> --use-partitioned-tables \
  --log-filter='(resource.type="cloud_run_revision" OR resource.type="cloud_run_job") AND jsonPayload.event:"memory."'

# 3. Let the sink write: add its writerIdentity (printed by `describe`) as a WRITER on the dataset ACL.
#    (`bq add-iam-policy-binding` fails on datasets with bq 2.1.39; use the ACL round-trip.)
gcloud logging sinks describe alek-memory-telemetry --project=<PROJECT_ID> --format='value(writerIdentity)'
bq --project_id=<PROJECT_ID> show --format=prettyjson alek_memory_dev > ds.json
#    edit ds.json: append {"role": "WRITER", "userByEmail": "<writerIdentity without the serviceAccount: prefix>"} to "access"
bq --project_id=<PROJECT_ID> update --source ds.json alek_memory_dev
```

BigQuery creates the table (`run_googleapis_com_stdout`) on the first matching record and widens its
schema as new fields appear. Analysis examples (SQL over `jsonPayload`) live in
`docs/05_building_blocks/memory_telemetry/README.md`. Knobs: `MEMORY_TELEMETRY=off` disables the sampler;
`MEMORY_SAMPLE_INTERVAL_S` (default 15, minimum 1) sets the idle interval — under pressure (≥ 75% of the
limit) it samples every second.

## Cost Optimization

| Strategy             | Cost/Month | Pros        | Cons                      |
| -------------------- | ---------- | ----------- | ------------------------- |
| **Scale-to-zero**    | $0         | Cheapest    | Cold starts (2-4s)        |
| **Keep-alive pings** | $0.10-1    | Almost free | May still get cold starts |
| **min-instances=1**  | $15-30     | Always warm | Expensive                 |

**Recommendation:** Start with keep-alive pings ($0.10/mo), upgrade to min-instances if needed.

---

## Node.js Dependencies in the Docker Image

The image bundles two independent Node.js projects for document generation. Both are installed
during the Docker build via `npm install --omit=dev`:

| Directory | npm package | Purpose | Notes |
|-----------|------------|---------|-------|
| `docx_generator/` | `docx` | DOCX file generation (NodeDocxRunner) | Lightweight; no system-level dependencies |
| `pdf_generator/` | `puppeteer ^24.x` | PDF rendering via headless Chromium (NodePuppeteerRunner) | Downloads bundled Chromium (~170 MB) during install |

`pdf_generator/node_modules/` is excluded from the Docker build context via `.dockerignore` — the
`npm install` step in the `Dockerfile` installs it fresh inside the image layer.

Because Puppeteer downloads Chromium at install time, the first `docker build` (or Cloud Build)
after a Puppeteer version change will be slow (~3–5 min for the download). Subsequent builds use
the Docker layer cache as long as `pdf_generator/package.json` is unchanged.

---

## Artifact Registry Image Cleanup

The `gcr.io` Artifact Registry repository stores Docker images pushed by `make deploy` via
`cloudbuild-dev.yaml`. Every deploy overwrites the `:latest` tag but leaves the previous digest as
an untagged orphan. Without active cleanup, storage accumulates unbounded: the repo was **~120 GB**
in July 2026 (before cleanup), with 136 dead prod images + 489 dev images, costing ~$10.71/month.

**Automatic cleanup via `set-cleanup-policies` does not work reliably** in the current gcloud CLI
version (578.0.0+), so cleanup is manual:

```bash
# Dry-run: see what would be deleted
python3 scripts/infra/cleanup_gcr_images.py --dry-run

# Actual cleanup: delete untagged images older than 7 days
python3 scripts/infra/cleanup_gcr_images.py
```

The script deletes all untagged images older than 7 days, keeping only recent builds (typically
~10 images = one week of roughly-daily manual deploys), plus any images explicitly tagged with
`:dev` or similar.

**Recommended cadence:** Run monthly or after a week of active development (when `alek-bot-dev`
accumulates untagged digests). The source of truth for the policy is
[`scripts/infra/gcr-io-cleanup-policy.json`](../../scripts/infra/gcr-io-cleanup-policy.json)
(currently a placeholder for future `set-cleanup-policies` adoption; the Python script is the
working implementation).

---

## Related Documentation

- **[06_runtime/README.md](../06_runtime/README.md)** - Runtime architecture
- **[05_building_blocks/](../05_building_blocks/)** - Component documentation
- **[12_risks/IMPLEMENTATION_ROADMAP.md](../12_risks/IMPLEMENTATION_ROADMAP.md)** - Development roadmap

---

**Last Updated:** 2026-03-14

## Cloud Tasks redelivery dedup

`docs/04_solution_strategy/decisions/worker_task_dedup.md`. Two one-time owner steps, neither blocks
a deploy:

1. **TTL policy on the task-claim collection** (per database). Each `agent_execution` delivery
   creates a claim document with `expires_at` 30 min after creation; without the policy they are
   never deleted:
   ```bash
   gcloud firestore fields ttls update expires_at --collection-group=<prefix>worker_task_dedup \
     --enable-ttl --database=us-production --project=<PROJECT_ID>
   ```
2. **Attempt logging on the agent queue**, so a redelivery can be told from a retry:
   ```bash
   gcloud tasks queues update agent-tasks-dev --location=us-central1 \
     --log-sampling-ratio=1.0 --project=<PROJECT_ID>
   ```
