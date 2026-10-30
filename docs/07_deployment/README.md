# Deployment

How to deploy Alek-Core the standard way from a fresh checkout, and keep deploying it.

- **Start with [`../../BOOTSTRAP.md`](../../BOOTSTRAP.md):** prerequisites, local setup, `.env`, tests, the first
  `make deploy` and the health check. This file does not repeat them.
- **This file:** every deploy unit, the one-time project setup the deploy needs, the two-pass first deploys, the rules
  that hold on every deploy, and the standing levers.
- One-off rollout steps of a single feature (migration orders, rollbacks of a release, first-run checks) are not kept
  here; they live in the rollout notes (ops).

Placeholders only (`<PROJECT_ID>`, `<SERVICE_URL>`, service accounts): the real values live in `.env`, Secret Manager or
memory, never in this file.

---

## Deploy units

The first four units share one image (`gcr.io/$PROJECT_ID/alek-bot-dev:latest`) and one build config
(`cloudbuild-dev.yaml`); one `make deploy` builds and deploys them all, and a unit differs only by `--command`/`--args`.
The sandbox render service has its own image (`Dockerfile.sandbox`) and is built by the same `make deploy`.

| Unit | Type | Region | Timeout | Scaling | Secrets it reads | `make` targets |
|------|------|--------|---------|---------|-------------------|-----------------|
| `alek-bot-dev` | Cloud Run service | us-central1 | 1800s | min=0, max=1 | ~30 (Slack, Telegram, LLM providers, OAuth, etc. — see cloudbuild-dev.yaml) | `logs`, `fetch-logs`, `logs-tail` |
| `alek-research-job-dev` | Cloud Run Job | us-central1 | task-timeout 18000s | on-demand (no min/max-instances — Jobs run to completion) | `ANTHROPIC_API_KEY`, `SERVICE_ACCOUNT_EMAIL` | `logs-job`, `fetch-logs-job`, `list-jobs`, `logs-execution`, `cancel-job` |
| `alek-voice-relay-dev` | Cloud Run service | us-central1 | **3600s** | min=0, max=1 | `OPENAI_API_KEY`, `XAI_API_KEY`, `BILLING_SLACK_WEBHOOK_URL` | `logs-relay`, `fetch-logs-relay` |
| `alek-bot-indexer-dev` | Cloud Run service (gen2, 2 GiB, concurrency 1, ingress internal) | us-central1 | 1800s | min=0, max=1 | `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `MICROSOFT_TODO_CLIENT_ID`, `MICROSOFT_TODO_CLIENT_SECRET`, `SERVICE_ACCOUNT_EMAIL`, `BILLING_SLACK_WEBHOOK_URL`, `DRIVE_INVOKER_SA_EMAIL`, `OIDC_TRANSITION_SA_EMAILS`, `KMS_CRYPTO_KEY` | `logs-indexer`, `fetch-logs-indexer` |
| `alek-bot-sandbox-render-dev` | Cloud Run service (gen2, 2 GiB, 2 CPU, concurrency 1, HTTP/2, no public invoker) | us-central1 | 300s | min=0, max=3 | **none** (zero-role identity, no secret) | (see Sandbox render service) |

`cloudbuild-dev.yaml` deploys the queue and the indexer after the main service, and the sandbox steps after the indexer, so a
failing drive or sandbox step never blocks a fix of the bot.

**Voice relay** (`relay_main.py`, a plain `websockets` server; [`../10_rfcs/VOICE_COMPANION_RFC.md`](../10_rfcs/VOICE_COMPANION_RFC.md)
§4.14). Its own service because an open WebSocket is one long billed request and needs the 3600 s timeout, which must not
apply to the main service.
- **`--allow-unauthenticated` is required:** Twilio's Media Streams WebSocket cannot present a Google OIDC token. The
  relay's own calls into the main service's `/voice/session-config` and `/voice/submit-transcript` stay OIDC-protected.
- **Identity.** The relay runs as the default runtime service account and mints an OIDC token from it. `main.py` accepts
  `{SERVICE_ACCOUNT_EMAIL, VOICE_RELAY_SA_EMAIL} ∪ OIDC_TRANSITION_SA_EMAILS` on `/voice/*` (`parse_sa_emails` /
  `build_oidc_gate`, `src/web/worker_oidc_verifier.py`). A separate relay service account works once
  `VOICE_RELAY_SA_EMAIL` (a Secret Manager secret holding its email) is mounted on the main service; without it every
  such call is 401. All three keys are optional: absent, the gate accepts exactly `SERVICE_ACCOUNT_EMAIL`.
- **No `TWILIO_*` secrets on the relay:** they belong to the main service's webhook and telephony adapter.
- **The realtime provider is per user, not an env setting** (`VOICE_MULTI_PROVIDER_RFC.md`): the relay holds both
  provider keys and builds each call's adapter from the call's session config. Switch a user with `voice_provider` in
  their `UserBotConfig` (`"xai"` / `"openai"`); the default is `DEFAULT_VOICE_PROVIDER` in
  `src/domain/voice_provider_profile.py`.

**Drive indexer** (`indexer_main.py`; [`../10_rfcs/USER_DRIVE_INDEXING_RFC.md`](../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9,
§12.5; [decision](../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md)). A `/worker` that serves
`drive_sync` only (plus `/health`); the main service (2 GiB) never indexes.
- **Ingress is internal and the in-app OIDC check stays:** a request from outside Google's network gets 404 before the
  app, and on Cloud Run the check fails closed when no accepted identity is configured (`DRIVE_INVOKER_SA_EMAIL`, else
  `SERVICE_ACCOUNT_EMAIL`, plus the optional `OIDC_TRANSITION_SA_EMAILS`; RFC Δ21).
- **The token audience is pinned** to the indexer's own URL (`{url}` or `{url}/worker`): a manual task sent to another
  run.app URL form than the one in `INDEXER_URL_DEV` gets 401.
- **Feed.** The scheduler job `alek-bot-dev-start-drive-sync` sends the main service `start_drive_sync` every 10 minutes
  (minutes 3, 13, …), which fans out one `drive_sync` per user; bot writes and the Cabinet toggle also enqueue one. All go
  through the queue `drive-sync-dev` (`max-concurrent-dispatches=1`, 1800 s dispatch deadline). Queue, job and the build
  identity's queue permission: [SCHEDULERS.md](SCHEDULERS.md) § Start Drive Sync.
- **Model keys.** The reader and triage agents mount `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` through `--set-secrets`
  (the same secret names the main service mounts), never as Cloud Build substitutions.
- **Logs** are Cloud Logging only (no Logfire): `make fetch-logs-indexer K=300` → `alek_debug_indexer.log`, or
  `make logs-indexer`. A finished tick logs one `[DriveIndex] tick user=… end=…` line on the indexer, never on the main
  service; each index run logs one `[DriveIndex] run …` line. "Why was file X not indexed" is answered from the record and
  these lines (building block [`../05_building_blocks/user_drive/README.md`](../05_building_blocks/user_drive/README.md) §7.1).

**Sandbox render service:** see [Sandbox render service](#sandbox-render-service) below.

---

## Deploy-only `.env` keys

`make deploy` reads `.env` through `include .env` (only when the file exists) and passes some keys as Cloud Build
substitutions. A key used that way bakes its value into the deployed revision, so a value that differs between local and
deploy gets a deploy-only key of its own, never a reuse of a local key (CLAUDE.md, "Deploy-substitution trap").

**Deploy only from a checkout that has the real `.env`.** Without it the build ships empty values into the live
environment and breaks OAuth and voice. A second working copy has no `.env` of its own: link the main checkout's file
first (`ln -s ../alekbot-core/.env .env`). Before every deploy, check what will be sent:
`make -n deploy | grep -o -- '--substitutions=[^ ]*'`. Every value must be non-empty, except `_INDEXER_URL` on the
indexer's first pass and the sandbox keys before the sandbox exists.

| `.env` key | Substitution → variable | Notes |
|---|---|---|
| `SERVICE_URL_DEV` | `_SERVICE_URL`, `_OAUTH_REDIRECT_URI` | `make deploy` refuses when empty |
| `VOICE_RELAY_URL_DEV` | `_VOICE_RELAY_URL` → `VOICE_RELAY_STREAM_URL` (main) | `https://` form; the Makefile turns it into `wss://`. Two-pass, below |
| `INDEXER_URL_DEV` | `_INDEXER_URL` → `DRIVE_INDEXER_URL` (both services) | no trailing slash; `make deploy` refuses when empty unless `DRIVE_FIRST_PASS=1`. Two-pass, below |
| `DRIVE_TRANSCRIBE_ENABLED_DEV` | `_DRIVE_TRANSCRIBE_ENABLED` → `DRIVE_TRANSCRIBE_ENABLED` | **required**, exactly `on` or `off` (below) |
| `DRIVE_TRANSCRIBE_USD_CAP_DEV` | `_DRIVE_TRANSCRIBE_USD_CAP` | optional; can only lower the $20 monthly cap (below) |
| `DRIVE_NOTES_ENABLED_DEV` | `_DRIVE_NOTES_ENABLED` → `DRIVE_NOTES_ENABLED` (both services) | optional; empty = `on`; `off` is the notes kill switch (Standing levers) |
| `SANDBOX_RENDER_DEV`, `SANDBOX_RENDER_URL_DEV`, `SANDBOX_RENDER_SA_DEV`, `SANDBOX_RENDER_CALLERS_DEV` | `_SANDBOX_RENDER*` | see Sandbox render service |

`DRIVE_FIRST_PASS=1` is a command-line flag for the indexer's first pass only; never put it in `.env`.

### Transcripts switch and cap

- `DRIVE_TRANSCRIBE_ENABLED_DEV` — `on` turns the indexer's transcription kill switch on, `off` turns it off; no other
  value is accepted. `make deploy` refuses unless it is exactly `on` or `off`, and echoes the effective switch and cap
  before it submits. It also reaches the main service, where the Cabinet shows the disclosure only if it is on. It is a
  substitution, not a by-hand variable, because `--set-env-vars` replaces every variable on each deploy. A manual
  `gcloud builds submit` without the Makefile deploys `off`.
- `DRIVE_TRANSCRIBE_USD_CAP_DEV` — optional number of dollars. It can only **lower** the $20 monthly cap (a larger or
  unparsable value is the default; empty = $20). Set on the indexer and on the main service (the Cabinet shows the
  effective cap). Without the key a lowered live cap silently rises to $20.
- Never use the local names `DRIVE_TRANSCRIBE_ENABLED` / `DRIVE_TRANSCRIBE_USD_CAP` for the deploy.
- A deploy with the switch `off` **freezes in-flight batches**: they stay at the provider with their reservations, and
  their cost is booked when transcription is switched back on.

---

## One-time project setup

Do these once per project or database, before the first deploy that needs them.

### Secrets the deploy mounts

`cloudbuild-dev.yaml`'s `--set-secrets` names every secret each unit mounts; **`gcloud run deploy` fails until each one
exists and the runtime service account can read it.** Create secrets with `printf '%s'`, never `echo`: a trailing newline
breaks the value (it broke an `Authorization` header and eight secrets before). Three need setup beyond the secret:

**Cloudflare Realtime SFU (web calls).** Web calls from the Cabinet (`docs/10_rfcs/VOICE_WEB_TRANSPORT_RFC.md`) reuse the
relay; only the main service needs credentials, to mint SFU sessions.
1. Create a Cloudflare Realtime app (Realtime → Calls SFU, or the TURN/Calls API section). Note the **App ID** and the
   **App Secret** (shown once).
2. Create the two secrets and grant access:
   ```bash
   printf '%s' "<APP_ID>" | gcloud secrets create CLOUDFLARE_SFU_APP_ID --data-file=- --project=<PROJECT_ID>
   printf '%s' "<APP_SECRET>" | gcloud secrets create CLOUDFLARE_SFU_APP_SECRET --data-file=- --project=<PROJECT_ID>
   for s in CLOUDFLARE_SFU_APP_ID CLOUDFLARE_SFU_APP_SECRET; do
     gcloud secrets add-iam-policy-binding "$s" --project=<PROJECT_ID> \
       --member="serviceAccount:<RUNTIME_SA_OF_THE_SERVICE_THAT_MOUNTS_IT>" --role=roles/secretmanager.secretAccessor
   done
   ```
3. A deployed revision always gets both keys (or the deploy fails); the "warn and skip the `/cabinet/call` blueprint"
   path in `main.py` applies only to local runs without them.
4. Two optional relay knobs, read with `os.environ.get`, both defaulting to the phone path's values:
   `VOICE_WEB_REASONING_EFFORT` (default `medium`) and `VOICE_WEB_CALLER_OPENING` (`on`/`off`, default `on`).

**OneDrive App Folder (user drive)** (`docs/10_rfcs/USER_DRIVE_RFC.md`). It reuses the To Do app registration
(`MICROSOFT_TODO_CLIENT_ID` / `_SECRET`); the adapter is wired only when both are set. The main service mounts
`ONEDRIVE_REDIRECT_URI_DEV` unconditionally.
1. Azure registration `Alek-bot`: add the delegated permission `Files.ReadWrite.AppFolder`.
2. Create the secret and grant access:
   ```bash
   printf '%s' "<SERVICE_URL>/auth/connect-onedrive/callback" | \
     gcloud secrets create ONEDRIVE_REDIRECT_URI_DEV --data-file=- --project=<PROJECT_ID>
   gcloud secrets add-iam-policy-binding ONEDRIVE_REDIRECT_URI_DEV --project=<PROJECT_ID> \
     --member="serviceAccount:<RUNTIME_SA_OF_THE_SERVICE_THAT_MOUNTS_IT>" --role=roles/secretmanager.secretAccessor
   ```
3. Register the same URI as a redirect URI (platform "Web") on the `Alek-bot` registration.
4. After the deploy, connect the drive from the Cabinet (Integrations, "Connect drive").

Local runs read `ONEDRIVE_REDIRECT_URI` from `.env` (a separate key, so a localhost value never reaches a deployed
revision). Unset, `/auth/connect-onedrive` answers 501.

**Agent workspace bucket** (`docs/10_rfcs/AGENT_WORKSPACE_RFC.md` §14). The main service mounts `GCS_WORKSPACE_BUCKET`.
1. Bucket in the main service's region: uniform access, public access prevention, soft delete off (versioning is the
   recovery; a soft-deleted copy would survive an erasure), versioning on, and this lifecycle:
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
2. IAM on that bucket only: `roles/storage.objectUser` for the main service's runtime service account (the indexer, relay
   and Jobs get nothing):
   ```bash
   gcloud storage buckets add-iam-policy-binding gs://<WORKSPACE_BUCKET> \
     --member=serviceAccount:<MAIN_SERVICE_RUNTIME_SA> --role=roles/storage.objectUser
   ```
3. The secret: `printf '%s' <WORKSPACE_BUCKET> | gcloud secrets create GCS_WORKSPACE_BUCKET --data-file=- --project=<PROJECT_ID>`,
   with `roles/secretmanager.secretAccessor` for the runtime SA if the project does not grant it broadly.

Local dev shares the live bucket (D9): with `GOOGLE_CLOUD_PROJECT` set, `load_settings()` fetches an empty
`GCS_WORKSPACE_BUCKET` from Secret Manager, and the developer's own account needs object access. Another bucket name in
`.env` points elsewhere; with the key empty and no project, no workspace is built.

### Twilio number (Lelik's number)

Set by hand in the Twilio Console (the REST `IncomingPhoneNumbers` write did not reliably reach live routing):
- **Active Region: United States (US1).** Webhook signatures use the auth token of the region that processes the call,
  and `TWILIO_AUTH_TOKEN` is the US1 token; in IE1 every `/voice/auth` is 403.
- **A call comes in:** Webhook, `https://<SERVICE_URL_DEV>/voice/auth`, POST.
- **Call status changes:** `https://<SERVICE_URL_DEV>/voice/inbound-status`, POST. The callback is placed only once the
  inbound dial reports `completed`; without this URL nobody ever calls back.

### Firestore indexes

`config/firestore.indexes.json` is the live database's index configuration: only `development_`-prefixed collections
appear in it (a test enforces the prefix).
- Composites and single-field exemptions: `make deploy-indexes PROJECT_ID=<PROJECT_ID>` (or
  `python scripts/infrastructure/deploy_firestore_indexes.py config/firestore.indexes.json <PROJECT_ID>`). An existing
  index answers "already exists": read the output, not only the exit code. Vector entries are skipped.
- Vector indexes: `PROJECT_ID=<PROJECT_ID> scripts/infrastructure/create_drive_vector_indexes.sh` (drive;
  `COLLECTION_PREFIX` defaults to `development_`) and `scripts/infrastructure/create_task_search_vector_indexes.sh`
  (task search). A vector index carries only the dimension (768) and `flat`; the distance measure is chosen per query.
- Both need the project id passed explicitly: `.env` has `GOOGLE_CLOUD_PROJECT`, not `PROJECT_ID`.
- **Wait for `READY`.** This prints the ones still building:
  ```bash
  gcloud firestore indexes composite list --database=us-production --project=<PROJECT_ID> \
    --format='table(name.basename(), state)' | grep -v READY
  ```
  `composite list` does not show field overrides; check an exemption with
  `gcloud firestore indexes fields describe <field> --collection-group=<collection> --database=us-production --project=<PROJECT_ID>`.
- **Smoke check:** `venv/bin/python scripts/infrastructure/drive_index_smoke.py <DEV_USER_ID>` runs every drive repository
  query once (read-only) and names any missing index.

### Firestore TTL policies

One-time, per database. Each collection below carries a Timestamp `expires_at`; without the policy, its expired records
are never deleted (nothing breaks).

| Collection group | What expires |
|---|---|
| `<prefix>voice_tickets` | tickets, the one-call marker, call-kind keys, late-answer records |
| `<prefix>long_turns` | long-turn registry records |
| `<prefix>skill_drafts` and `draft_files` | unsaved skill drafts (30 days) and their staged file contents |
| `<prefix>memory_fact_drafts` | unconfirmed memory-fact drafts (7 days) |
| `<prefix>worker_task_dedup` | Cloud Tasks claims (1 h), daily-review marks (48 h), deep-research webhook claims (72 h) |
| `<prefix>drive_model_usage` | monthly drive model-usage documents (the `{user}:initial` document has no `expires_at`) |

```bash
gcloud firestore fields ttls update expires_at --collection-group=<collection-group> \
  --enable-ttl --database=us-production --project=<PROJECT_ID>
```

Two collections expire on a different field, **`purge_after`**, which exists only in storage:

| Collection group | What expires |
|---|---|
| `<prefix>fact_review_questions` | closed memory-review questions (90 days after closing; an open question has no `purge_after`) |
| `<prefix>fact_review_messages` | weekly-digest claims (90 days after the claim) and quote translations (180 days after they were written) |

```bash
gcloud firestore fields ttls update purge_after --collection-group=<collection-group> \
  --enable-ttl --database=us-production --project=<PROJECT_ID>
```

Without these policies nothing breaks, but the janitor posts a 🚨 alert on each run while a closed record is 3 days past
its `purge_after`.

- **A policy is keyed by collection-group ID across the whole database, not by path.** That is why a draft's file
  subcollection is `draft_files` and never `files`: a saved skill's own files live in a `files` subcollection, which a
  policy on a draft subcollection of the same name would also cover.
- **Never a TTL policy on the facts collection** (`<prefix>domain_facts_v2`) or on the janitor's
  `<prefix>fact_lifecycle_daily` / `<prefix>fact_lifecycle_lines`: a fact's `expires_at` is a decision (the janitor stales,
  never deletes), so a policy would erase facts on the day they expire. `tests/unit/test_facts_have_no_ttl_policy.py`
  fails on such a command anywhere in the tree. `<prefix>memory_write_leases` has no policy either (its expiry field is
  `lease_until`), nor do `<prefix>fact_lifecycle_run` and `<prefix>fact_review_state` (their expiry fields are
  decisions, not deletions).

### Memory telemetry sink

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

### Cloud Tasks queues

- Slack events go to `alek-bot-tasks-{env}`; Telegram updates (`telegram_update`), `send_to_chat` errands and other agent
  tasks go to `agent-tasks-{env}` (us-central1) and share its 5 concurrent dispatch slots. `dispatch_deadline` is set per
  task, not on the queue.
- Attempt logging on the agent queue, so a redelivery can be told from a retry:
  ```bash
  gcloud tasks queues update agent-tasks-dev --location=us-central1 \
    --log-sampling-ratio=1.0 --project=<PROJECT_ID>
  ```
- The drive queue, every scheduler job and the deadlines: [SCHEDULERS.md](SCHEDULERS.md).

### Personal-data gate (GitHub)

`.github/workflows/pii_gate.yml` checks every PR (CLAUDE.md, "PERSONAL DATA RULE") against the private denylist and
fails closed until one repository secret exists (Settings → Secrets and variables → Actions):
- `PII_DENYLIST` — the private denylist's content (`hard|soft <regex>` per line).

CI prints counts only (the repository is public, so is the Actions log). To see which lines hit, run the check locally:
`PII_DENYLIST_FILE=<path to the private denylist> python3 -m scripts.pii_gate.denylist_check --range origin/main...HEAD`.

The semantic model check is local-only and never runs in CI: put `PII_PROFILE` and `ANTHROPIC_API_KEY` in the local
environment and run `python3 -m scripts.pii_gate.semantic_check --range origin/main...HEAD`. Its findings are leads for
a stronger-model review, not verdicts.

A false positive is overridden with the PR label `pii-ok` (create it once). PRs from forks get no secrets and fail.

**Stored-secrets encryption (`docs/10_rfcs/SECRETS_AT_REST_RFC.md`).** The main service mounts two more secrets and refuses to
start without either (locally too); the indexer mounts `KMS_CRYPTO_KEY` and refuses to start without it (it refreshes OneDrive
tokens through the credential store, `decisions/credential_store_cutover.md`).
1. A Cloud KMS key ring and one software key with purpose `ENCRYPT_DECRYPT`, automatic rotation every 90 days, data-access
   audit logs on for Cloud KMS. Its full resource name is the secret `KMS_CRYPTO_KEY`
   (`projects/<PROJECT_ID>/locations/<LOCATION>/keyRings/<KEY_RING>/cryptoKeys/<KEY>`).
2. `SECRET_DIGEST_KEY`: at least 32 random bytes as hex (`openssl rand -hex 32`, stored with `printf '%s'`), the key of the
   verify-only digests. Changing it invalidates every digested value still in flight (MCP codes live ~10 minutes).
3. Grant `roles/cloudkms.cryptoKeyEncrypterDecrypter` **on the key** (not the project) to the **main service's** and the **indexer's**
   service accounts (each service runs under its own identity), and to the developer's own identity: local runs use the real key,
   with no fallback cipher. The relay, sandbox and deep-research job get no key grant. `KMS_CRYPTO_KEY` needs
   `secretmanager.secretAccessor` for both accounts, `SECRET_DIGEST_KEY` for the main service's.
Keep the digest key's value out of tracked files and chat; it is not derivable from the KMS key.

---

## First deploy: the two-pass URLs

Three units publish a URL that another unit needs, and the URL exists only after the unit's first deploy. Each is deployed
twice once; after that the URL is stable and `.env` keeps it.

### Voice Relay Stream URL — two-pass first deploy

The main service hands Twilio the relay's WebSocket URL (`<Connect><Stream url="wss://…">` in `/voice/answer`) from
`VOICE_RELAY_STREAM_URL`. **Until `VOICE_RELAY_URL_DEV` is set, every call fails with an empty stream URL.**
1. `make deploy` creates `alek-voice-relay-dev` (the main service ships with an empty stream URL on this pass).
2. Read the URL back:
   ```bash
   gcloud run services describe alek-voice-relay-dev \
     --region=us-central1 --format='value(status.url)'
   ```
3. Put it in `.env` as `VOICE_RELAY_URL_DEV=<that https:// URL>` (keep `https://`; the deploy converts it).
4. `make deploy` again.

### Indexer URL — two-pass first deploy

Both services read the indexer's URL from `DRIVE_INDEXER_URL` (an optional knob, never in `load_settings()`): the main
service targets the drive queue at it, and the indexer enqueues its continuations there and pins its token audience to
it. **While it is empty, no drive task queue is built and indexing never runs:**
- the main service logs `Drive sync queue: disabled` at INFO on startup and answers `start_drive_sync` with
  `200 {"status": "disabled"}`; the nudges are skipped; the Cabinet toggle saves the flag, enqueues nothing and logs a WARNING;
- the indexer logs `DRIVE_INDEXER_URL is not set` at WARNING, and its `/worker` answers 503 to a `drive_sync` (ERROR).

0. **Once:** the identity Cloud Build runs as needs `roles/cloudtasks.queueAdmin` on the drive queue
   ([SCHEDULERS.md](SCHEDULERS.md) § Start Drive Sync). **Indexes first**: create the drive composites and the
   `card_vector` vector indexes (§ Firestore indexes above), wait until all are `READY` (the tick's own `due_records`
   query needs them, not only search), then run the smoke check.
1. `DRIVE_FIRST_PASS=1 make deploy` creates the queue, `alek-bot-indexer-dev` and the scheduler job, with an empty
   `DRIVE_INDEXER_URL` on both services.
2. Read the URL back:
   ```bash
   gcloud run services describe alek-bot-indexer-dev --region=us-central1 --format='value(status.url)'
   ```
3. Put it in `.env` as `INDEXER_URL_DEV=<that https:// URL>` (no trailing slash; its own key, RFC Δ21). Manual tasks must
   use this same URL (the pinned audience).
4. `make deploy` again, then check:
   ```bash
   gcloud run services describe alek-bot-indexer-dev --region=us-central1 --format=yaml | grep -A1 DRIVE_INDEXER_URL
   gcloud run services describe alek-bot-dev --region=us-central1 --format=yaml | grep -A1 DRIVE_INDEXER_URL
   gcloud tasks queues describe drive-sync-dev --location=us-central1 \
     --format='value(rateLimits.maxConcurrentDispatches)'            # 1
   gcloud scheduler jobs describe alek-bot-dev-start-drive-sync --location=us-central1 \
     --format='value(schedule,state)'                                 # 3-59/10 * * * *  ENABLED
   curl -s -o /dev/null -w '%{http_code}\n' -X POST "<indexer URL>/worker" -d '{}'   # 404 (ingress), never 2xx/5xx
   ```
   On this second pass a task may meet the indexer's 503 for a minute or two; the queue retries it.

### Sandbox render service

`alek-bot-sandbox-render-dev` runs `render.docx` (the Node script a model wrote for a DOCX) for the main service
([`../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4, §7 S1, §8;
[decision](../04_solution_strategy/decisions/sandbox_render_service.md)). It is `sandbox_main.py` in its own image
(`Dockerfile.sandbox`, target `render`; an explicit COPY allowlist, no `.env`, no key file): gen2, 2 GiB, 2 CPU,
concurrency 1, max 3, timeout 300 s, `--use-http2`, `--no-allow-unauthenticated`, an in-memory volume at `/work` (size
fixed in `scripts/infrastructure/cloudbuild_sandbox_render.sh`; changed by editing that script, not through `.env`). One
request per instance (`SANDBOX_RECYCLE=always`). The main service uses it only when `SANDBOX_RENDER=sandbox`; the default
is `inprocess`.

| `.env` key | Substitution | Meaning |
|---|---|---|
| `SANDBOX_RENDER_DEV` | `_SANDBOX_RENDER` | the switch on the **main** service: `inprocess` (default when empty) or `sandbox` |
| `SANDBOX_RENDER_URL_DEV` | `_SANDBOX_RENDER_URL` | the render service's own `https://` URL: the main service's target and the service's token audience (`SANDBOX_URL`) |
| `SANDBOX_RENDER_SA_DEV` | `_SANDBOX_RENDER_SA` | the render service's runtime identity; **empty skips every sandbox build step** |
| `SANDBOX_RENDER_CALLERS_DEV` | `_SANDBOX_RENDER_CALLERS` | emails admitted to `render.*` (`:`-separated; a comma is refused) |

**Owner steps, in order** (nothing here is run by an agent):
1. **Create the identity and grant it nothing.** Its name must start with `alek-sandbox-`: the deploy script refuses any
   other `SANDBOX_RENDER_SA_DEV` with exit 3, so a mis-set value cannot give the "holds nothing" service real IAM. Verify
   that `gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings[].members --filter="bindings.members:<sandbox-render-sa>"`
   prints nothing.
2. **Let the deployer act as it:** `roles/iam.serviceAccountUser` on `<sandbox-render-sa>` (that account only) for the
   build's deployer identity, so `gcloud run deploy --service-account=<sandbox-render-sa>` is allowed.
3. **Put the keys in `.env`:** `SANDBOX_RENDER_SA_DEV=<sandbox-render-sa>` and `SANDBOX_RENDER_CALLERS_DEV=<main-runtime-sa>`.
   Leave `SANDBOX_RENDER_URL_DEV` empty and `SANDBOX_RENDER_DEV` unset. The callers must be set **before the first
   deploy**: an empty caller set on Cloud Run refuses the revision.
4. **Pass one: `make deploy`.** The service starts with `SANDBOX_URL` empty, serves **only `/health`**, and answers every
   op with `503 unconfigured`.
5. **Read the URL back** and put it in `.env` as `SANDBOX_RENDER_URL_DEV=<https:// URL>`:
   ```bash
   gcloud run services describe alek-bot-sandbox-render-dev --region=us-central1 --format='value(status.url)'
   ```
6. **Grant invoker to the main runtime only:** `roles/run.invoker` on the render service to `<main-runtime-sa>`; never
   `allUsers` or `allAuthenticatedUsers`. Audit once and after each deploy that neither the project nor the service policy
   contains them (RFC §8 step 2).
7. **Pass two: `make deploy`.** The render service now knows its audience; the main service has the URL. The switch is
   still `inprocess`.
8. **Flip the switch** only on the owner's go:
   `SANDBOX_RENDER_DEV=sandbox` in `.env`, then `make deploy`. With `sandbox` and an empty URL on Cloud Run the main service
   still starts (ERROR logged) and DOCX creation reports the sandbox unavailable; locally it is a startup error.

While `SANDBOX_RENDER_SA_DEV` is empty, `cloudbuild_sandbox_render.sh` exits 0 in every mode, so `make deploy` works
before step 1.

---

## The drive specialist (`work_with_drive_files`)

Smart reaches the drive through one LLM specialist (`docs/10_rfcs/DRIVE_SPECIALIST_RFC.md` §10, §13;
`decisions/drive_specialist.md`). It adds no service, secret or Cloud Build substitution: the specialist runs in the main
service on the Claude and OpenAI keys it already has, so the ordinary `make deploy` is the whole code change.

Its prompt lives in Firestore like every other agent's (tokens `COGNITIVE_PROCESS_DRIVE`, `PROTOCOL_DRIVE_SEARCH`,
`OUTPUT_FORMAT_DRIVE`, blueprint `drive_agent_v1`, profile `drive`). There is no fallback prompt: a missing `drive`
profile fails every commission with "PromptBuilder failed" (`preload_cache("drive")` only warns at startup). The owner
uploads the prompt layer before the release whose code needs it ("Every deploy" below), and `PROTOCOL_AGENT_SELECTION`,
which names `work_with_drive_files`, goes up right before that release, not earlier.

---

## Every deploy

- **`make deploy`** builds and deploys every unit. Deploy is manual by choice
  ([decision](../04_solution_strategy/decisions/ci_present_cd_deliberately_absent.md)). It reads the **working tree, not
  git**: uncommitted changes ship.
- **Indexes before code.** A new composite in `config/firestore.indexes.json` must be `READY` before the code that queries
  it is deployed: a missing or building index fails the query with `FailedPrecondition` (on the indexer, a store outage
  that ends the tick). Issue it with `make deploy-indexes`, wait, run the smoke check, then deploy.
- **Prompts are not deployed.** Tokens, blueprints and profiles live in Firestore; the owner uploads them with
  `firestore_utils/snapshot_upload.py` ([`../../prompts_snapshot/README.md`](../../prompts_snapshot/README.md); back up the
  prompt layer first, never from an agent session). Upload before the deploy whose code needs them: the prompt assembly
  caches a template for 24 h per instance, so fresh instances must find the new token.
- **`--set-env-vars` replaces every variable.** A variable set by hand with `gcloud run services update` is gone at the
  next deploy; a lasting setting goes into `cloudbuild-dev.yaml` or a deploy-only `.env` key.
- **A deploy never resumes a paused queue or scheduler job.** Resume them yourself.
- **Check what serves** after a deploy:
  ```bash
  gcloud run services describe <service> --region=us-central1 --project=<PROJECT_ID> \
    --format='value(status.latestReadyRevisionName,spec.template.spec.containers[0].image)'
  ```

---

## Standing levers

**Roll back the main service.** Note the serving revision before a deploy
(`gcloud run services describe alek-bot-dev --region=us-central1 --project=<PROJECT_ID> --format='value(status.traffic[0].revisionName)'`);
roll back with `gcloud run services update-traffic alek-bot-dev --region=us-central1 --project=<PROJECT_ID> --to-revisions=<prev>=100`.
A release that changes stored data, indexes or prompts has its own rollback order in its rollout notes (ops).

**Roll back the drive specialist.** Revert the merge, and restore `PROTOCOL_AGENT_SELECTION` right before the revert's
release, never earlier: a protocol restored first runs the new code with the old text. The order is standing text in
`DRIVE_SPECIALIST_RFC.md` §13 (summarised in `decisions/drive_specialist.md` § Rollback); the one-off commands with the
release's own values are in its rollout notes (ops).

**The `:latest` trap.** After a failed deploy or a traffic rollback, `:latest` still points at the new image. Every
`gcloud run services update`, and every Makefile target that wraps it (warm/cold, `claude-`/`openai-`/`dr-rollback` and
`-forward`), creates a new revision from that image; with traffic pinned to the old revision it gets 0 %, so a kill-switch
silently does nothing, and a later `--to-latest` serves the bad image. Run no `services update` and no Makefile switch until
the fix is deployed forward; if one is unavoidable, follow it with an explicit `update-traffic`.

**Drive indexing.**
- Pause / resume the drive work: `gcloud tasks queues pause|resume drive-sync-dev --location=us-central1 --project=<PROJECT_ID>`
  and `gcloud scheduler jobs pause|resume alek-bot-dev-start-drive-sync --location=us-central1 --project=<PROJECT_ID>`. A
  running tick keeps going for up to 30 minutes after a pause.
- Stop every upgrade pass: `gcloud run services update alek-bot-indexer-dev --update-env-vars DRIVE_UPGRADE_PASS=off
  --region=us-central1 --project=<PROJECT_ID>`. **It does not survive a deploy** (`--set-env-vars`): set it again after each
  deploy while it is needed.
- Stop model spend only: remove `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the indexer's secrets; every model call is then
  a model outage (never charged to a file), and parsing, cards and search go on. The Gemini fallback does not take over
  (`GEMINI_API_KEY` stays for embeddings); to turn the fallback off, set `"fallback": None` in the drive entries of
  `AgentProviderStrategy.STRATEGIES`.
- Stop all content work: empty `ADMITTED_KINDS` and `TRIAGED_KINDS` in `src/domain/drive_index_policy.py` and bump
  `POLICY_VERSION` (`src/domain/drive_index.py`); the upgrade pass collects every readable chunk.
- One user: turn their indexing off in the Cabinet; the purge removes hints, chunks, records and sync state.
- Rebuild one user: `venv/bin/python scripts/infrastructure/drive_index_rebuild.py --user <USER_ID>` (dry run; `--apply`
  marks the sync state `purging` under the user's lease, refused while a tick runs). The next tick purges and re-onboards.
  `--all-users` visits every sync state; `--hints-only` deletes hint documents only.
- Re-read files of one user after a fix:
  `venv/bin/python scripts/infrastructure/drive_content_requeue.py --user <USER_ID> [--reason extract_error | --truncated | --no-text] [--kind <kind>]`
  (dry run: counts by kind and selection, how many stay held, how many go to the reader and are billed; `--apply` makes
  the selected records due again under the user's lease, refused while a tick runs). `--reason`: `failed` records with
  that reason; `--truncated`: indexed records whose text was cut by an older chunk cap; `--no-text`: images with a `text`
  verdict the reader got no text from. The next tick reads them; no `EXTRACTION_VERSION` bump, so the rest of the drive
  is not re-read.
- Transcription: the user's flag off (`scripts/infrastructure/drive_transcribe_flag.py --user USER --off --apply`) stops
  that user's new work and the mirror folder; the kill switch off (`DRIVE_TRANSCRIBE_ENABLED_DEV=off`, redeploy) stops it
  for everyone and freezes in-flight batches. There is no mirror-only switch.

**Drive notes** (`docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md` §3.6). Stop every note write: `DRIVE_NOTES_ENABLED_DEV=off` in `.env`
and `make deploy` (it echoes `_DRIVE_NOTES_ENABLED=` before it submits). One substitution sets the main service and the
indexer together: no note tool for the drive specialist, the `$drive-notes` commands answer "not available", and the
indexer does no notes work. Reads go on (the open header, the deleted-files search, copies already in cards). Never use
the local name `DRIVE_NOTES_ENABLED` for the deploy, and never set it by hand with `services update` (`--set-env-vars`
replaces it at the next deploy).

**Sandbox render.** Back to in-process: `SANDBOX_RENDER_DEV=inprocess` (or empty) and `make deploy`; the render service can
stay up unused. **Hard stop:** remove the caller's `roles/run.invoker` binding on the render service. Never empty
`SANDBOX_RENDER_CALLERS_DEV` to stop it: an empty caller list refuses the new revision, so the old one keeps serving.

**Agent workspace erasure** (no user-facing path; owner command, removes every generation — soft delete is off, so nothing
remains): `gcloud storage rm --all-versions "gs://<WORKSPACE_BUCKET>/<USER_ID>/**"`.

**Fact janitor.** Stop it at once: `gcloud scheduler jobs pause alek-bot-dev-fact-lifecycle-sweep --location=us-central1
--project=<PROJECT_ID>`. It writes since the deploy of 2026-10-27 (`FACT_LIFECYCLE_SWEEP_DRY_RUN=0` in the main
service's `--set-env-vars`); the rollback lever is removing that variable (a deploy: `--set-env-vars` replaces every variable), which makes it log-only again ([memory map](../05_building_blocks/memory/README.md) §5.5; [SCHEDULERS.md](SCHEDULERS.md) § Fact Lifecycle Sweep).

**Memory telemetry.** `MEMORY_TELEMETRY=off` disables the sampler; `MEMORY_SAMPLE_INTERVAL_S` sets the idle interval
(§ Memory telemetry sink above).

---

## Operations

**Status and logs.**
```bash
gcloud run services describe alek-bot-dev --region=us-central1   # the main service
gcloud scheduler jobs list --location=us-central1                # scheduler jobs
make logs                     # recent main-service logs; make fetch-logs K=300 → alek_debug.log
make logs-relay | make logs-indexer | make logs-job              # the other units (fetch-logs-* variants write a file)
```
Log queries and the Logfire cascade: [LOGGING.md](LOGGING.md). Alert policies: [ALERTING.md](ALERTING.md).

**Keep-alive.** Scale-to-zero costs nothing but cold starts (2–4 s); a keep-alive ping costs cents a month;
`min-instances=1` keeps it warm at a real monthly cost. Setup: [KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md)
(`./scripts/infrastructure/setup-keep-alive.sh` with `GCP_PROJECT_ID` and `CLOUD_RUN_URL` exported).

**Image cleanup.** Every deploy moves `:latest` and leaves the previous digest untagged, so the `gcr.io` repository grows
without bound. `set-cleanup-policies` does not work reliably in the current gcloud CLI, so clean up by hand, monthly or
after a busy week:
```bash
python3 scripts/infra/cleanup_gcr_images.py --dry-run   # what would be deleted
python3 scripts/infra/cleanup_gcr_images.py             # delete untagged images older than 7 days
```
[`scripts/infra/gcr-io-cleanup-policy.json`](../../scripts/infra/gcr-io-cleanup-policy.json) is a placeholder for a future
`set-cleanup-policies`; the script is the working implementation.

**Node.js in the image.** `docx_generator/` (`docx`) and `pdf_generator/` (`puppeteer`, downloads Chromium, ~170 MB) are
installed in the Docker build with `npm install --omit=dev`; `pdf_generator/node_modules/` is excluded by `.dockerignore`.
The first build after a Puppeteer version change takes a few minutes longer; later builds use the layer cache while
`pdf_generator/package.json` is unchanged.

---

## Related guides

- [SCHEDULERS.md](SCHEDULERS.md) — every Cloud Scheduler job and task type: schedule, payload, deadlines
- [KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md) — keep-alive pings against scale-to-zero
- [LOGGING.md](LOGGING.md) — structured logging, Cloud Logging queries, Logfire
- [ALERTING.md](ALERTING.md) — alert policies and logging exclusions
- [../../BOOTSTRAP.md](../../BOOTSTRAP.md) — from-scratch setup
