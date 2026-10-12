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

All four units share one image (`gcr.io/$PROJECT_ID/alek-bot-dev:latest`) and one build config
(`cloudbuild-dev.yaml`, deployed together by a single `make deploy`); a job/service is distinguished
only by `--command`/`--args` at deploy time.

| Unit | Type | Region | Timeout | Scaling | Secrets it reads | `make` targets |
|------|------|--------|---------|---------|-------------------|-----------------|
| `alek-bot-dev` | Cloud Run service | us-central1 | 1800s | min=0, max=1 | ~30 (Slack, Telegram, LLM providers, OAuth, etc. — see cloudbuild-dev.yaml) | `logs`, `fetch-logs`, `logs-tail` |
| `alek-research-job-dev` | Cloud Run Job | us-central1 | task-timeout 18000s | on-demand (no min/max-instances — Jobs run to completion) | `ANTHROPIC_API_KEY`, `SERVICE_ACCOUNT_EMAIL` | `logs-job`, `fetch-logs-job`, `list-jobs`, `logs-execution`, `cancel-job` |
| `alek-voice-relay-dev` | Cloud Run service | us-central1 | **3600s** | min=0, max=1 | `OPENAI_API_KEY`, `XAI_API_KEY`, `BILLING_SLACK_WEBHOOK_URL` | `logs-relay`, `fetch-logs-relay` |
| `alek-bot-indexer-dev` | Cloud Run service (gen2, 2 GiB, concurrency 1, ingress internal) | us-central1 | 1800s | min=0, max=1 | `GEMINI_API_KEY`, `MICROSOFT_TODO_CLIENT_ID`, `MICROSOFT_TODO_CLIENT_SECRET`, `SERVICE_ACCOUNT_EMAIL` | `logs-indexer`, `fetch-logs-indexer` |

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
- **No dedicated service account** — the relay runs as the same default compute service account as
  `alek-bot-dev`/`alek-research-job-dev` (neither of those overrides `--service-account` either).
  This is required, not just simplest: `relay_main.py` mints an OIDC identity token from its own
  Cloud Run runtime identity to call the main service, and `main.py` checks that token with the exact
  same `verify_worker_oidc(token, expected_sa_email=config.get("SERVICE_ACCOUNT_EMAIL"))` gate used
  for `/worker` — one shared expected email for every caller. A separate relay service account would
  401 every one of those calls unless `SERVICE_ACCOUNT_EMAIL` were also changed, which is out of
  scope here.
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
   echo -n "<APP_ID>" | gcloud secrets create CLOUDFLARE_SFU_APP_ID --data-file=- --project=<PROJECT_ID>
   echo -n "<APP_SECRET>" | gcloud secrets create CLOUDFLARE_SFU_APP_SECRET --data-file=- --project=<PROJECT_ID>
   for s in CLOUDFLARE_SFU_APP_ID CLOUDFLARE_SFU_APP_SECRET; do
     gcloud secrets add-iam-policy-binding "$s" --project=<PROJECT_ID> \
       --member="serviceAccount:<SERVICE_ACCOUNT_EMAIL>" --role=roles/secretmanager.secretAccessor
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
   echo -n "<SERVICE_URL>/auth/connect-onedrive/callback" | \
     gcloud secrets create ONEDRIVE_REDIRECT_URI_DEV --data-file=- --project=<PROJECT_ID>
   gcloud secrets add-iam-policy-binding ONEDRIVE_REDIRECT_URI_DEV --project=<PROJECT_ID> \
     --member="serviceAccount:<SERVICE_ACCOUNT_EMAIL>" --role=roles/secretmanager.secretAccessor
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
404 before it reaches the app, and on Cloud Run the check fails closed when `SERVICE_ACCOUNT_EMAIL` is unset (RFC
Δ21). The token audience is pinned to the indexer's own URL (`{url}` or `{url}/worker`), so a manual task sent to a
run.app URL form other than the one in `INDEXER_URL_DEV` gets 401. The main service (1 GiB) never indexes. The
scheduler job `alek-bot-dev-start-drive-sync` sends it `start_drive_sync` every 10 minutes (minutes 3, 13, …), and
it fans out one `drive_sync` per user; it also nudges after bot writes, and the Cabinet toggle enqueues one. All of
them go through the dedicated queue `drive-sync-dev` (`max-concurrent-dispatches=1`) with a 1800 s dispatch
deadline. Queue, scheduler job and the Cloud Build service account's queue permission
(`roles/cloudtasks.queueAdmin`, granted once): [SCHEDULERS.md](SCHEDULERS.md). `cloudbuild-dev.yaml` deploys the
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

0. **Once:** the Cloud Build service account needs `roles/cloudtasks.queueAdmin`
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

USER_DRIVE_INDEXING_RFC §24 adds model calls and chunk storage to the indexer. Before the deploy that ships it:

1. **Spikes, recorded in the RFC (§18).** S1 (child-process limits hold on the indexer for the Phase 0 bomb set; peak
   RSS with a 45 MB download; cold start) before the parser ships; S0g (Graph custom renditions `c{w}x{h}` for App
   Folder items, legible receipt text, HEIC) before the reader reads images; S3 (`find_nearest` cost and latency at
   10k and 50k chunks under a `user_id` filter) before the slice ships.
2. **Model keys on the indexer.** The reader and triage agents call Claude (default) or OpenAI, so the indexer mounts
   `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` from Secret Manager (`--set-secrets` in `cloudbuild-dev.yaml`, the same
   secret names the main service mounts). They are never Cloud Build substitutions. The runtime service account
   already reads them for the main service; check it once with
   `gcloud secrets get-iam-policy ANTHROPIC_API_KEY --project=<PROJECT_ID>`.
3. **Prompts.** The `drive_reader` and `drive_triage` tokens, blueprints and profiles are uploaded by the owner
   (`docs/how_to/NEW_AGENT_PLAYBOOK.md`; never from an agent session). Without them every model call fails as a model
   outage (logged ERROR): parsed documents are still indexed, scans and images wait.
4. **Smart's drive guidance.** The `drive_specialist` block of `PROTOCOL_AGENT_SELECTION`
   (`prompts_snapshot/tokens/system/PROTOCOL_AGENT_SELECTION.groovy`): back up the prompt layer
   (`firestore_utils/backup_prompt_layer.py` into `scripts/memory/`), check the mirror for drift
   (`python firestore_utils/snapshot_pull.py --check`), dry-run the upload, then `--apply` (owner only;
   [prompts_snapshot/README.md](../../prompts_snapshot/README.md)).
5. **Indexes.** The chunk collection `development_drive_index_chunks` needs its composite indexes, and the records
   collection the `unread` counts' composites (three of them carry `triage_verdict`, for images triaged as text)
   (`make deploy-indexes PROJECT_ID=<PROJECT_ID>`) and its vector indexes on the chunk vector under `user_id` and
   `user_id + kind` (`PROJECT_ID=<PROJECT_ID> scripts/infrastructure/create_drive_vector_indexes.sh`). Wait for
   `READY`, then run the smoke check, which now also runs the chunk queries.
6. **Cost.** A page read by the model costs about $0.0006 on Haiku 5.5 or GPT-6 Luna; the monthly budget bounds a user
   at about $3 (§24.9). The calls are billed to the user's account and appear in `billing_daily_summary`; the tick
   summary line carries `model_pages`, `model_calls`, `triage_images` and `model_cost`.

**Rebuild one user's index.** `venv/bin/python scripts/infrastructure/drive_index_rebuild.py --user <USER_ID>` is a
dry run; add `--apply` to mark the sync state `purging` under the user's lease (refused while a tick runs: retry in a
few minutes). The next tick purges the index and re-onboards from epoch 1 (USER_DRIVE_INDEXING_RFC §12.2).

**Logs.** The indexer has no Logfire telemetry: Cloud Logging only. A tick that ends `done` or `continued` logs one
`[DriveIndex] tick user=… end=… {…} duration=…` line on the indexer, not on the main service, so `make fetch-logs`
does not show it: `make fetch-logs-indexer K=300` writes `alek_debug_indexer.log` (`make logs-indexer` prints
instead). A throttled tick logs a WARNING and a failed one an ERROR instead of that line; a purge logs
`index purged`. An item whose observation fails inside a page is skipped with one ERROR line. That is expected
behaviour (§8.2), not an incident, unless the same item repeats on every tick.

**Until the branch is merged**, a `make deploy` from another branch removes `start_drive_sync` from the main service
while the scheduler job keeps firing. Pause the job first:
`gcloud scheduler jobs pause alek-bot-dev-start-drive-sync --location=us-central1`.
After the next deploy from this branch, resume it:
`gcloud scheduler jobs resume alek-bot-dev-start-drive-sync --location=us-central1`. A deploy does not resume a
paused job.

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
