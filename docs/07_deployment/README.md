<!-- living-doc
covers:
  Makefile
  Dockerfile*
  .dockerignore
  .env.example
  cloudbuild*.yaml
  requirements*.txt
  scripts/infra/
  scripts/infrastructure/artifact_registry_retention.py
  scripts/infrastructure/check_env_drift.py
  scripts/infrastructure/cloudbuild_sandbox_light.sh
  scripts/infrastructure/cloudbuild_sandbox_render.sh
  scripts/infrastructure/deploy_firestore_indexes.py
  scripts/generate_ttl_runbook.py
  src/composition/deploy_topology.py
  .github/workflows/pii_gate.yml
  scripts/pii_gate/
  .githooks/
  scripts/git-hooks/
  scripts/install-git-hooks.sh
-->

# Deployment

How to deploy Alek-Core the standard way from a fresh checkout, and keep deploying it.

- **Start with [`../../BOOTSTRAP.md`](../../BOOTSTRAP.md):** prerequisites, local setup, `.env`, tests, the first
  `make deploy` and the health check. This file does not repeat them.
- **This file:** every deploy unit, the deploy-only `.env` keys, the one-time project setup the deploy needs, the
  two-pass first deploys, the rules that hold on every deploy, the standing levers and the per-service knobs.
- One-off rollout steps of a single feature (migration orders, rollbacks of a release, first-run checks) are not kept
  here; they live in the rollout notes (ops).

`.dockerignore` keeps `.env` and key files out of the image; `.env.example` lists the keys a checkout needs;
`src/composition/deploy_topology.py::DeployTopology` is the one place that derives the service URL, queue suffix and regions.

Placeholders only (`<PROJECT_ID>`, `<SERVICE_URL>`, service accounts): the real values live in `.env`, Secret Manager or
memory, never in this file. Resource values (memory, timeouts, scaling, secret lists) live in `cloudbuild-dev.yaml`,
`scripts/infrastructure/cloudbuild_sandbox_light.sh`, `scripts/infrastructure/cloudbuild_sandbox_render.sh` and the
`Makefile`; this file says what they mean.

---

## Deploy units

Six units. The first four share one image (`gcr.io/$PROJECT_ID/alek-bot-dev:latest`, `Dockerfile`) and one build config
(`cloudbuild-dev.yaml`); a unit differs by its own service account, `--command`/`--args` (not the main service), env, secrets and resource limits. The two sandbox services
have their own image file (`Dockerfile.sandbox`, one target each) and are built by the same `make deploy`.

| Unit | Kind | Entry point | Why it is its own unit |
|---|---|---|---|
| `alek-bot-dev` | Cloud Run service | `main.py` | the bot: chat, web, `/worker`, voice control plane |
| `alek-research-job-dev` | Cloud Run Job | `job_main.py` | deep research runs to completion, hours long |
| `alek-voice-relay-dev` | Cloud Run service | `relay_main.py` | one open WebSocket is one long billed request; needs its own request timeout |
| `alek-bot-indexer-dev` | Cloud Run service, ingress internal | `indexer_main.py` | `drive_sync` ticks need their own memory and one-tick-per-instance concurrency; the main service never indexes |
| `alek-bot-sandbox-render-dev` | Cloud Run service, no public invoker | `sandbox_main.py` (`Dockerfile.sandbox` target `render`) | runs model-written DOCX and HTML code with no secret and no egress |
| `alek-bot-sandbox-light-dev` | Cloud Run service, no public invoker | `sandbox_main.py` (target `light`) | parses untrusted uploaded and drive files, decodes recordings, with no secret and no egress |

Log targets: `make logs`, `logs-relay`, `logs-indexer`, `logs-job` and their `fetch-logs*` variants (the Makefile's `##` help lines).
Each unit's secrets are exactly its `--set-secrets` line in `cloudbuild-dev.yaml` (the sandbox services mount none:
`--clear-secrets`). The main service runs `--execution-environment=gen2` and parses no file itself: documents go through
the sandbox light service (`SANDBOX_PARSE_URL`, from `_SANDBOX_LIGHT_URL`).

The shared image installs `requirements.txt` only: what the four entry points import, no file-parsing library (those run
in the sandbox images, `requirements-sandbox*.txt`). Tests, CI and local scripts install `requirements-dev.txt`.
`tests/unit/test_main_image_requirements_closure.py` fails when code reachable from an entry point imports a distribution
`requirements.txt` does not provide.

**Step graph (`cloudbuild-dev.yaml`, ids and `waitFor`).** The main service deploys first (pull, build, push, deploy,
public-invoker binding) and depends on nothing drive- or relay-related. Only after `main-public-iam` do the independent
branches run in parallel: the drive queue then the indexer and its binding, the research job, the relay and its binding,
and the scheduler script (after the queue it feeds). The sandbox light and render families each run serially and wait for
all four branches (the two families do not wait for each other). A failing drive, relay or sandbox step therefore never
blocks a fix of the bot.

**Deploy order and the sandbox light service.** The main service deploys before the sandbox light service, so a revision
that uses a new `parse.*` op reaches the main service before the sandbox serves it. For `parse.container_facts` (the macro
check on email sends) that is a brief window in which every ZIP-family attachment send is refused as "could not be
checked" (fail closed, no unchecked send); it ends when the light deploy finishes.

**Voice relay** ([`../10_rfcs/VOICE_COMPANION_RFC.md`](../10_rfcs/VOICE_COMPANION_RFC.md) §4.14).
- **`--allow-unauthenticated` is required:** Twilio's Media Streams WebSocket cannot present a Google OIDC token. The
  relay's own calls into the main service's `/voice/session-config` and `/voice/submit-transcript` stay OIDC-protected.
- **Identity.** The relay runs as its own service account and mints an OIDC token from it. `main.py` accepts
  `{SERVICE_ACCOUNT_EMAIL, VOICE_RELAY_SA_EMAIL} ∪ OIDC_TRANSITION_SA_EMAILS` on `/voice/*` (`parse_sa_emails` /
  `build_oidc_gate`, `src/web/worker_oidc_verifier.py`). A separate relay service account works once `VOICE_RELAY_SA_EMAIL`
  (a Secret Manager secret holding its email) is mounted on the main service; without it every such call is 401. All
  three keys are optional: absent, the gate accepts exactly `SERVICE_ACCOUNT_EMAIL`.
- **No `TWILIO_*` secrets on the relay:** they belong to the main service's webhook and telephony adapter.
- **The realtime provider is per user, not an env setting** (`VOICE_MULTI_PROVIDER_RFC.md`): the relay holds both provider
  keys and builds each call's adapter from the call's session config. Switch a user with `voice_provider` in their
  `UserBotConfig`; the default is `src/domain/voice_provider_profile.py::DEFAULT_VOICE_PROVIDER`.
- **`--max-instances=1` is a pairing constraint** (both SFU legs of one ticket must land on one process,
  `VOICE_WEB_TRANSPORT_RFC.md` §5.4): scale calls with relay shards, never by raising it.

**Drive indexer** ([`../10_rfcs/USER_DRIVE_INDEXING_RFC.md`](../10_rfcs/USER_DRIVE_INDEXING_RFC.md) §9, §12.5;
[decision](../04_solution_strategy/decisions/drive_indexer_lease_queue_service.md)). A `/worker` that serves `drive_sync`
only, plus `/health`.
- **Ingress is internal and the in-app OIDC check stays:** a request from outside Google's network gets 404 before the
  app, and on Cloud Run the check fails closed when no accepted identity is configured (`DRIVE_INVOKER_SA_EMAIL`, else
  `SERVICE_ACCOUNT_EMAIL`, plus the optional `OIDC_TRANSITION_SA_EMAILS`; `src/web/indexer_worker_app.py::worker_oidc_gate`).
- **The token audience is pinned** to the indexer's own URL (`indexer_audiences`: `{url}` or `{url}/worker`): a manual task
  sent to another run.app URL form than the one in `INDEXER_URL_DEV` gets 401.
- **Feed.** The scheduler job `alek-bot-dev-start-drive-sync` sends the main service `start_drive_sync`, which fans out one
  `drive_sync` per drive connection (payload `{user_id, connection_id}`; a manual task needs the connection id too); bot
  writes and the Cabinet toggle also enqueue one. All go through the queue `drive-sync-dev`, provisioned by the
  `drive-queue` step with `max-concurrent-dispatches=1` (a CPU/memory knob; the per-user Firestore lease is the
  correctness mechanism). Schedule, queue and the build identity's queue permission: [SCHEDULERS.md](SCHEDULERS.md)
  § Start Drive Sync.
- **Model keys.** The reader and triage agents mount `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` through `--set-secrets`
  (the same secret names the main service mounts), never as Cloud Build substitutions.
- **Logs** are Cloud Logging only (no Logfire): `make fetch-logs-indexer K=300` → `alek_debug_indexer.log`, or
  `make logs-indexer`. A finished tick logs one `[DriveIndex] tick user=… end=…` line on the indexer, never on the main
  service; each index run logs one `[DriveIndex] run …` line. "Why was file X not indexed" is answered from the record and
  these lines (building block [`../05_building_blocks/user_drive/README.md`](../05_building_blocks/user_drive/README.md) §7.1).

**Sandbox services:** see [Sandbox network](#sandbox-network-no-egress), [Sandbox render service](#sandbox-render-service)
and [Sandbox light (parse) service](#sandbox-light-parse-service).

---

## Deploy-only `.env` keys

`make deploy` reads `.env` through `include .env` (only when the file exists) and passes some keys as Cloud Build
substitutions. A key used that way bakes its value into the deployed revision, so a value that differs between local and
deploy gets a deploy-only `*_DEV` key of its own, never a reuse of a local key (CLAUDE.md, "Deploy-substitution trap").
The Makefile maps each `*_DEV` key to a `_SUBSTITUTION`, and `cloudbuild-dev.yaml` maps it to the runtime variable.

**Deploy only from a checkout that has the real `.env`.** Without it the build ships empty values into the live
environment and breaks OAuth and voice. A second working copy has no `.env` of its own: link the main checkout's file
first (`ln -s ../alekbot-core/.env .env`). Before every deploy, check what will be sent:
`make -n deploy | grep -o -- '--substitutions=[^ ]*'`. Every value must be non-empty, except the optional ones (`_DRIVE_TRANSCRIBE_USD_CAP`,
`_VOICE_DEBUG_TRANSCRIPT_USERS`), `_VOICE_RELAY_URL` / `_INDEXER_URL` on their first passes and the sandbox render keys and
`_SANDBOX_LIGHT_SA` / callers before the sandbox exists (the light URL and both VPC keys are never empty: `make deploy`
refuses).

| `.env` key | Substitution → variable | Notes |
|---|---|---|
| `SERVICE_URL_DEV` | `_SERVICE_URL`, `_OAUTH_REDIRECT_URI` | `make deploy` refuses when empty. Its host is also the passkey RP ID: a new domain makes every passkey useless, and users re-enrol after a Google sign-in (CABINET_DEVICE_SESSIONS_RFC §10) |
| `VOICE_RELAY_URL_DEV` | `_VOICE_RELAY_URL` → `VOICE_RELAY_STREAM_URL` (main) | `https://` form; the Makefile turns it into `wss://`. Two-pass, below |
| `GCS_MEDIA_BUCKET_DEV` | `_GCS_MEDIA_BUCKET` → `GCS_MEDIA_BUCKET` (main service and research job) | **required**; `make deploy` refuses when empty |
| `INDEXER_URL_DEV` | `_INDEXER_URL` → `DRIVE_INDEXER_URL` (both services) | no trailing slash; `make deploy` refuses when empty unless `DRIVE_FIRST_PASS=1`. Two-pass, below |
| `DRIVE_TRANSCRIBE_ENABLED_DEV` | `_DRIVE_TRANSCRIBE_ENABLED` → `DRIVE_TRANSCRIBE_ENABLED` | **required**, exactly `on` or `off` (below) |
| `DRIVE_TRANSCRIBE_USD_CAP_DEV` | `_DRIVE_TRANSCRIBE_USD_CAP` | optional; can only lower the monthly cap (below) |
| `DRIVE_UPGRADE_PASS_DEV` | `_DRIVE_UPGRADE_PASS` → `DRIVE_UPGRADE_PASS` (indexer) | optional; empty = `on`; `off` holds every version upgrade pass (Standing levers) |
| `DRIVE_NOTES_ENABLED_DEV` | `_DRIVE_NOTES_ENABLED` → `DRIVE_NOTES_ENABLED` (both services) | optional; empty = `on`; `off` is the notes kill switch (Standing levers) |
| `EMAIL_CARD_ONLINE_DEV` | `_EMAIL_CARD_ONLINE` → `EMAIL_CARD_ONLINE` (main) | optional; empty = `1`; `0` turns the email card step off across deploys (Standing levers) |
| `VOICE_DEBUG_TRANSCRIPT_USERS_DEV` | `_VOICE_DEBUG_TRANSCRIPT_USERS` → `VOICE_DEBUG_TRANSCRIPT_USERS` (relay) | optional, debug only; empty = off; `;`-separated user ids (relay knob 8) |
| `SANDBOX_RENDER_URL_DEV`, `SANDBOX_RENDER_SA_DEV`, `SANDBOX_RENDER_CALLERS_DEV` | `_SANDBOX_RENDER*` | see Sandbox render service |
| `SANDBOX_VPC_NETWORK_DEV`, `SANDBOX_VPC_SUBNET_DEV` | `_SANDBOX_VPC_NETWORK`, `_SANDBOX_VPC_SUBNET` | **required**: the custom VPC and its subnet both sandbox services deploy onto with no egress; `make deploy` refuses when either is empty, and the sandbox deploy scripts exit 4 (see Sandbox network) |
| `SANDBOX_LIGHT_SA_DEV`, `SANDBOX_LIGHT_URL_DEV`, `SANDBOX_LIGHT_CALLERS_MAIN_DEV`, `SANDBOX_LIGHT_CALLERS_INDEXER_DEV` | `_SANDBOX_LIGHT_*` | `SANDBOX_LIGHT_URL_DEV` is required by the deploy target (it becomes `SANDBOX_PARSE_URL` on the main service and the indexer); see Sandbox light (parse) service |

`DRIVE_FIRST_PASS=1` is a command-line flag for the indexer's first pass only; never put it in `.env`.

### Transcripts switch and cap

- `DRIVE_TRANSCRIBE_ENABLED_DEV` — `on` turns the indexer's transcription kill switch on, `off` turns it off; no other
  value is accepted. The Makefile default is `off`. `make deploy` refuses unless it is exactly `on` or
  `off`, and echoes the effective switch and cap before it submits. It also reaches the main service, where the Cabinet
  shows the disclosure only if it is on. It is a substitution, not a by-hand variable, because `--set-env-vars` replaces
  every variable on each deploy. A manual `gcloud builds submit` without the Makefile deploys `off`
  (`cloudbuild-dev.yaml` `substitutions`).
- `DRIVE_TRANSCRIBE_USD_CAP_DEV` — optional number of dollars. It can only **lower** the monthly cap
  (`src/domain/drive_index_params.py::TRANSCRIBE_USD_PER_MONTH`; a larger or unparsable value, or empty, is the default).
  Set on the indexer and on the main service (the Cabinet shows the effective cap). Without the key a lowered live cap
  silently rises to the default.
- Never use the local names `DRIVE_TRANSCRIBE_ENABLED` / `DRIVE_TRANSCRIBE_USD_CAP` for the deploy.
- A deploy with the switch `off` **freezes in-flight batches**: they stay at the provider with their reservations, and
  their cost is booked when transcription is switched back on.

---

## One-time project setup

Do these once per project or database, before the first deploy that needs them.

### Secrets the deploy mounts

`cloudbuild-dev.yaml`'s `--set-secrets` names every secret each unit mounts; **`gcloud run deploy` fails until each one
exists and the runtime service account can read it.** Create secrets with `printf '%s'`, never `echo`: a trailing newline
breaks the value (it broke an `Authorization` header). Several need setup beyond the secret:

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
   path in `main.py` applies only to a run without them or without `VOICE_RELAY_STREAM_URL`.
4. Optional relay knobs, read with `os.getenv` at the relay's entry point (`relay_main.py`), never in `load_settings()`:
   `VOICE_WEB_REASONING_EFFORT` (unset = each call's own spec; set, it overrides every web call whatever the provider, so
   it must be a value that provider accepts); `VOICE_WEB_CALLER_OPENING` (`off`: web calls ignore the call-opening note the main service puts in the session config and start with the
   pickup note); `VOICE_DEAD_PEER_TIMEOUT_S` (`relay_main._dead_peer_timeout_s`, default
   `src/handlers/sfu_stream_handler.py::DEAD_PEER_TIMEOUT_S`: a web call with no inbound audio packet for that long ends
   itself, `VOICE_CALL_CONTROL_CHANNEL_RFC.md` §7.4; a value that is not a positive number keeps the default; lowering it
   far risks ending backgrounded calls, because iOS pauses the web app's audio in the background); `LIVE_TEXT_LAG_MS`
   (`relay_main._live_text_lag_ms`: delays the release of Lelik's text on interpreter and tutor calls by that much audio,
   `docs/10_rfcs/VOICE_CALL_LIVE_TEXT_RFC.md` §4.3; an invalid value gives 0 with a warning).
5. One optional main-service knob, read with `os.getenv` per request: `VOICE_CONTROL_CHANNEL` (`on`/`off`; **`off` in
   code**), the kill switch of the call control channel (`docs/10_rfcs/VOICE_CALL_CONTROL_CHANNEL_RFC.md` §9). The deploy
   sets it `on` (`cloudbuild-dev.yaml` `--set-env-vars`); the rollback is
   `gcloud run services update <main service> --update-env-vars=VOICE_CONTROL_CHANNEL=off` until the next deploy. Off,
   `/connect` hands out no `control` and the page opens no socket; the relay needs no knob.
6. One optional relay secret for the call page's `geo` button (`docs/10_rfcs/GEO_MARKS_RFC.md` §4.7):
   `GOOGLE_SEARCH_API_KEY`, the same Secret Manager secret the main service mounts, bound to the relay in
   `cloudbuild-dev.yaml` and read with `os.getenv` at boot (`relay_main._reverse_geocoder`). The relay's service account
   needs `roles/secretmanager.secretAccessor` on it before the relay deploys, or the revision fails to start. Unset or
   empty, the call still takes the shared position and its note names no address. Deploy the relay before the main service
   when the control channel is first switched on for `geo`.
7. **Relay knob 8: `VOICE_DEBUG_TRANSCRIPT_USERS`** (debug only, off by default; `relay_main._voice_debug_transcript_users`):
   user ids separated by `;` (a comma breaks `--set-env-vars` and the substitution). A listed user's calls, in every mode,
   log `voice debug <fingerprint>:` INFO lines: turn events, notes by kind, tool calls with their arguments and outputs,
   session updates, the Ask latch, the language pair, and **the text of both sides' speech**. That carries third parties'
   speech (the other person on an interpreter call) into Cloud Logging: list a user only while debugging, then empty the key
   and redeploy. The ids are never logged (the boot line gives only the count). The deploy sets it from the deploy-only key
   `VOICE_DEBUG_TRANSCRIPT_USERS_DEV`.

**OneDrive App Folder (user drive)** (`docs/10_rfcs/USER_DRIVE_RFC.md`). It reuses the To Do app registration
(`MICROSOFT_TODO_CLIENT_ID` / `_SECRET`); the adapter is wired only when both are set.
1. Azure registration `Alek-bot`: add the delegated permission `Files.ReadWrite.AppFolder`.
2. The redirect is the shared `OAUTH_CONNECT_REDIRECT_URI` (below); there is no OneDrive-specific key.
3. After the deploy, connect the drive from the Cabinet (Integrations, "Connect drive").

**Google Drive (user drive)** (`docs/10_rfcs/GOOGLE_DRIVE_RFC.md` §8). The Google OAuth client is the one the main service
already mounts (`GOOGLE_OAUTH_CLIENT_ID` / `_SECRET`); the indexer mounts the same two secrets to refresh Google Drive
tokens. Both services build the Google adapter only when both values are present; without them the kind is simply not
registered and every other kind runs. The consent screen carries the Drive scope and the client the shared redirect (below).
1. The indexer runs as its own service account, so it needs a per-secret read grant (the deploy fails until it can read
   both secrets):
   ```bash
   for s in GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET; do
     gcloud secrets add-iam-policy-binding "$s" --project=<PROJECT_ID> \
       --member="serviceAccount:<INDEXER_SA_EMAIL>" --role=roles/secretmanager.secretAccessor
   done
   ```

**Integration connect redirect (`OAUTH_CONNECT_REDIRECT_URI`).** One callback serves every integration connect, Google and
Microsoft alike: `<SERVICE_URL>/auth/connect/callback` (`docs/10_rfcs/MULTI_DRIVE_RFC.md` §5.2). Deployed, the key comes
from the secret `OAUTH_CONNECT_REDIRECT_URI_DEV` mounted as `OAUTH_CONNECT_REDIRECT_URI` (`cloudbuild-dev.yaml`
`--set-secrets`); the secret is created and granted to the runtime SA like the other `_DEV` secrets. Register the same URI
as an authorized redirect URI on the Google OAuth client and as a redirect URI (platform "Web") on the `Alek-bot`
registration. Local runs read the key from `.env` (a localhost value never reaches a deployed revision: the deployed value
comes from the `_DEV` secret). A connect for a kind whose realm has no OAuth client configured answers 501.

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

Local dev shares the live bucket: with `GOOGLE_CLOUD_PROJECT` set, `load_settings()` fetches an empty
`GCS_WORKSPACE_BUCKET` from Secret Manager, and the developer's own account needs object access. Another bucket name in
`.env` points elsewhere; with the key empty and no project, no workspace is built.

**Stored-secrets encryption (`docs/10_rfcs/SECRETS_AT_REST_RFC.md`).** The main service mounts two more secrets and refuses
to start without either (locally too); the indexer mounts `KMS_CRYPTO_KEY` and refuses to start without it (it refreshes
OneDrive tokens through the credential store, `decisions/credential_store_cutover.md`).
1. A Cloud KMS key ring and one software key with purpose `ENCRYPT_DECRYPT`, automatic rotation, data-access audit logs on
   for Cloud KMS. Its full resource name is the secret `KMS_CRYPTO_KEY`
   (`projects/<PROJECT_ID>/locations/<LOCATION>/keyRings/<KEY_RING>/cryptoKeys/<KEY>`).
2. `SECRET_DIGEST_KEY`: at least 32 random bytes as hex (`openssl rand -hex 32`, stored with `printf '%s'`), the key of the
   verify-only digests. Changing it invalidates every digested value still in flight (MCP codes live minutes).
3. Grant `roles/cloudkms.cryptoKeyEncrypterDecrypter` **on the key** (not the project) to the **main service's** and the
   **indexer's** service accounts (each service runs under its own identity), and to the developer's own identity: local
   runs use the real key, with no fallback cipher. The relay, sandbox and deep-research job get no key grant.
   `KMS_CRYPTO_KEY` needs `secretmanager.secretAccessor` for both accounts, `SECRET_DIGEST_KEY` for the main service's.
Keep the digest key's value out of tracked files and chat; it is not derivable from the KMS key.

**Voice MCP token kind (`docs/10_rfcs/LELIK_VOICE_MCP_RFC.md` §5).** The main service mounts `VOICE_MCP_SIGNING_KEY`, the
HS256 key of Lelik's per-call MCP tokens and nothing else. **Create it before the first deploy that mounts it** (the deploy
fails otherwise): at least `SIGNING_KEY_MIN_CHARS` characters (`src/services/voice_mcp_token_service.py`; `openssl rand
-hex 32` is enough, stored with `printf '%s'`), never the OAuth session secret in effect nor the public dev placeholder
(`src/composition/voice_mcp_setup.py::signing_key` refuses the start for either), `secretmanager.secretAccessor` for the
main service's account only. Rotating it ends every live voice token at once (calls in flight lose their memory and drive
tools). The knob `VOICE_MCP_TOOLS` (`os.getenv`, main service only; **off in code**; the deploy sets it `on` through
`cloudbuild-dev.yaml` `--set-env-vars`; rollback is `--update-env-vars=VOICE_MCP_TOOLS=off` until the next deploy) wires
the tools: the plain companion call on xAI gets the memory and drive MCP servers as session tools (their URLs come from
`MCP_RESOURCE_URI`), loses its `search_memory` delegation, and `/voice/session-config` mints the call's tokens; with it on
and the key missing the main service refuses to start. The relay needs no knob: it offers a server only when the session
config carries both its descriptor and its token. **Upload `VOICE_TOOL_DATA_LELIK` and the `lelik_xai` profile before the
deploy that turns the knob on** (`prompts_snapshot/`, token upload). With the knob off nothing is minted, no call carries
the servers and `search_memory` stays; tokens made elsewhere with the key still verify until their call record is deleted.

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
  `python scripts/infrastructure/deploy_firestore_indexes.py config/firestore.indexes.json <PROJECT_ID> <DATABASE>`; the
  make target passes `FIRESTORE_DATABASE` from `.env`, and a missing database is refused: no live default). An existing
  index answers "already exists": read the output, not only the exit code. Vector entries are skipped.
- Vector indexes (every `create_*vector_index*.sh` refuses to run without `FIRESTORE_DATABASE`; `.env` has it, source it
  or pass it): `PROJECT_ID=<PROJECT_ID> FIRESTORE_DATABASE=<DATABASE> scripts/infrastructure/create_drive_vector_indexes.sh`
  (drive; `COLLECTION_PREFIX` defaults to `development_`) and `scripts/infrastructure/create_task_search_vector_indexes.sh`
  (task search). A vector index carries only the dimension and `flat`; the distance measure is chosen per query.
- The direct python call and the vector scripts need the project id passed explicitly (`.env` has `GOOGLE_CLOUD_PROJECT`,
  not `PROJECT_ID`); `make deploy-indexes` derives it itself.
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

<!-- ttl-runbook:begin (generated by scripts/generate_ttl_runbook.py from config/ttl_registry.json; do not edit) -->

| Collection group | What expires |
|---|---|
| `<prefix>voice_tickets` | tickets, the one-call marker, call-kind keys, late-answer records |
| `<prefix>long_turns` | long-turn registry records |
| `<prefix>skill_drafts` and `draft_files` | unsaved skill drafts (30 days) and their staged file contents |
| `<prefix>memory_fact_drafts` | unconfirmed memory-fact drafts (7 days) |
| `<prefix>worker_task_dedup` | Cloud Tasks claims (1 h), daily-review marks (48 h), external task API per-worker rate windows (two windows after the window starts) |
| `<prefix>drive_model_usage` | monthly drive model-usage documents (the `{user}:initial` document has no `expires_at`) |
| `<prefix>oauth_flows` | server-side integration connect flows (10 minutes); the ephemeral store reads the expiry itself, the policy only sweeps abandoned flows |
| `<prefix>geo_mark_links` | one-time `$geo` location links (10 minutes); the ephemeral store reads the expiry itself, the policy only sweeps unused links |
| `<prefix>event_dedup` | inbound event dedup claims (1 h); reads check expiry themselves |
| `<prefix>auth_passkey_challenges` | pending passkey ceremonies (5 minutes, single use); the ephemeral store reads the expiry itself, the policy only sweeps abandoned ones |
| `<prefix>drive_note_erase_requests` | pending drive-note erase requests |
| `<prefix>short_links` | short links (`resolve` re-checks expiry itself, the sweep lags) |
| `<prefix>mcp_auth_codes` | MCP authorization codes (about 10 minutes) |
| `<prefix>mcp_refresh_tokens` | expired MCP refresh tokens (an absent token is refused like an expired one) |

```bash
gcloud firestore fields ttls update expires_at --collection-group=<collection-group> \
  --enable-ttl --database=us-production --project=<PROJECT_ID>
```

Seven collections expire on a different field, **`purge_after`**, which exists only in storage:

| Collection group | What expires |
|---|---|
| `<prefix>auth_device_sessions_v1` | ended or expired Cabinet device sessions (the record is kept for a grace period after its absolute end or its revocation) |
| `<prefix>fact_review_questions` | closed memory-review questions (90 days after closing; an open question has no `purge_after`) |
| `<prefix>fact_review_messages` | weekly-digest claims (90 days after the claim) and quote translations (180 days after they were written) |
| `<prefix>audio_transcripts` | cached transcripts of opened chat audio (90 days after they were written; chat uploads themselves are deleted after 30 days by the bucket lifecycle) |
| `<prefix>drive_links` | the drive connector's one-time download links (a day after issue; a link itself dies 5 minutes after issue, checked on read) |
| `<prefix>external_tasks` | external task records (30 days after the task reaches a final state; a task still open has no `purge_after`) |
| `<prefix>external_task_results` | the stored result text of a done external task (30 days after the task is done, the same instant as its task record) |

```bash
gcloud firestore fields ttls update purge_after --collection-group=<collection-group> \
  --enable-ttl --database=us-production --project=<PROJECT_ID>
```

<!-- ttl-runbook:end -->

Without these policies nothing breaks, but the janitor posts an alert on each run while a closed record is
`src/domain/fact_lifecycle_sweep.py::PURGE_OVERDUE_AFTER` past its `purge_after`.

<!-- ttl-unverified:begin (generated by scripts/generate_ttl_runbook.py from config/ttl_registry.json; do not edit) -->

Collections whose code writes an expiry but whose policy is not in this runbook (owner: confirm one exists, or add it to the registry):

| Collection | Field | Note |
|---|---|---|

<!-- ttl-unverified:end -->

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

BigQuery creates the table (`run_googleapis_com_stdout`) on the first matching record and widens its schema as new fields
appear. Analysis examples (SQL over `jsonPayload`) live in `docs/05_building_blocks/memory_telemetry/README.md`. Knobs, read
with `os.getenv` in `src/composition/memory_telemetry_setup.py`: `MEMORY_TELEMETRY=off` disables the sampler;
`MEMORY_SAMPLE_INTERVAL_S` sets the idle interval (base and pressure values: `src/domain/memory_telemetry.py`).

### Cloud Tasks queues

- Slack events go to `alek-bot-tasks-{env}` (`src/composition/slack_adapter_factory.py`); Telegram updates, `send_to_chat`
  errands and other agent tasks go to `agent-tasks-{env}` (`src/composition/operations_setup.py`) and share its dispatch
  slots (`src/adapters/gcp_task_queue.py` creates a missing queue with 5). `dispatch_deadline` is set per task, not on the
  queue.
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

The same secret must also exist in the **Dependabot** store (`gh secret set PII_DENYLIST --app dependabot`): a PR opened
by Dependabot runs its workflows with Dependabot secrets, not Actions secrets, so without the copy the gate fails closed
on every dependency bump. The gate is a required status check in the `main` ruleset: a red gate blocks the merge.

CI prints counts only (the repository is public, so is the Actions log). To see which lines hit, run the check locally:
`PII_DENYLIST_FILE=<path to the private denylist> python3 -m scripts.pii_gate.denylist_check --range origin/main...HEAD`.

The semantic model check is local-only and never runs in CI: put `PII_PROFILE` and `ANTHROPIC_API_KEY` in the local
environment and run `python3 -m scripts.pii_gate.semantic_check --range origin/main...HEAD`. Its findings are leads for
a stronger-model review, not verdicts. The commit hooks (`.githooks/`, installed by `scripts/install-git-hooks.sh`) run
the same gate locally.

A false positive is overridden with the PR label `pii-ok` (create it once). PRs from forks get no secrets and fail.

---

## First deploy: the two-pass URLs

Four units publish a URL that another unit needs, and the URL exists only after the unit's first deploy. Each is deployed
twice once; after that the URL is stable and `.env` keeps it. The relay and indexer are below; the render and light
sandbox services follow the same pass-one / read-URL / pass-two shape in their own sections.

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
- the main service logs `Drive sync queue: disabled` at INFO on startup (`src/composition/drive_indexing_setup.py`) and
  answers `start_drive_sync` with `200 {"status": "disabled"}`; the nudges are skipped; the Cabinet toggle saves the flag,
  enqueues nothing and logs a WARNING;
- the indexer logs `DRIVE_INDEXER_URL is not set` at WARNING (`indexer_main.py`), and its `/worker` answers 503 to a
  `drive_sync` (ERROR).

0. **Once:** the identity Cloud Build runs as needs `roles/cloudtasks.queueAdmin` on the drive queue
   ([SCHEDULERS.md](SCHEDULERS.md) § Start Drive Sync); without it the `drive-queue` step fails the build after the main
   service is deployed, and the indexer and scheduler steps do not run. **Indexes first**: create the drive composites and
   the `card_vector` vector indexes (§ Firestore indexes above), wait until all are `READY` (the tick's own `due_records`
   query needs them, not only search), then run the smoke check.
1. `DRIVE_FIRST_PASS=1 make deploy` creates the queue, `alek-bot-indexer-dev` and the scheduler job, with an empty
   `DRIVE_INDEXER_URL` on both services.
2. Read the URL back:
   ```bash
   gcloud run services describe alek-bot-indexer-dev --region=us-central1 --format='value(status.url)'
   ```
3. Put it in `.env` as `INDEXER_URL_DEV=<that https:// URL>` (no trailing slash). Manual tasks must use this same URL
   (the pinned audience).
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
   On this second pass the main service gets `DRIVE_INDEXER_URL` a minute or two before the indexer does, so a task may
   meet the indexer's 503; the queue retries it, and the "/worker 5xx" alert may fire once.

### Sandbox network (no egress)

Both sandbox services deploy with Direct VPC egress `all-traffic` into a custom VPC that has no route to the internet
([RFC](../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4.5): `--network`, `--subnet` and `--vpc-egress=all-traffic` come from
`SANDBOX_VPC_NETWORK_DEV` and `SANDBOX_VPC_SUBNET_DEV` (`_SANDBOX_VPC_*`), and the two sandbox deploy scripts
exit 4 when either is empty, so a deploy never silently keeps open egress. Neither service needs the network: parse and
render input travels in the request, media ranges come back over the caller's tunnel, and Chromium aborts every non-data
URL. The metadata server and Google APIs (logging, image pull) keep working through Private Google Access.

**One-time owner setup** (placeholder names; nothing here is run by an agent):
1. A custom-mode VPC `<sandbox-vpc>` with one subnet `<sandbox-subnet>` (a /26 in us-central1) with Private Google Access on.
2. A Cloud DNS private zone on `<sandbox-vpc>` mapping `googleapis.com` to `private.googleapis.com` (`199.36.153.8/30`).
3. Firewall rules on `<sandbox-vpc>`: deny all egress (low priority) and allow egress `tcp:443` to `199.36.153.8/30` (higher priority).
4. **No Cloud NAT** and no external-IP route.
5. `.env`: `SANDBOX_VPC_NETWORK_DEV=<sandbox-vpc>`, `SANDBOX_VPC_SUBNET_DEV=<sandbox-subnet>`; the deployer needs
   `roles/compute.networkUser` on the subnet.

### Sandbox render service

`alek-bot-sandbox-render-dev` runs `render.docx` (the Node script a model wrote for a DOCX) and `render.pdf` (model-written
HTML) for the main service ([`../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4,
§8; [decision](../04_solution_strategy/decisions/sandbox_render_service.md)). It is `sandbox_main.py` in its own image
(`Dockerfile.sandbox`, target `render`; an explicit COPY allowlist, no `.env`, no key file), one request per instance
(`SANDBOX_RECYCLE=always`), `--no-allow-unauthenticated`, an in-memory volume at `/work` (size fixed in
`scripts/infrastructure/cloudbuild_sandbox_render.sh`, changed by editing that script, not through `.env`). The image
carries Python, Node, the `docx` package and Puppeteer's chrome-headless-shell in a root-owned `PUPPETEER_CACHE_DIR` the
engine user cannot write. **No render knob** (a `SANDBOX_RENDER` value is ignored): the main service sends DOCX and PDF to
this service whenever `SANDBOX_RENDER_URL` is set, and degrades them (ERROR logged, no start failure, locally too) when it
is empty (`src/composition/sandbox_client_setup.py`). Widget PNGs render in the main process, with Playwright Chromium; the
main image carries no Node, npm, puppeteer or ffmpeg.

| `.env` key | Substitution | Meaning |
|---|---|---|
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
   Leave `SANDBOX_RENDER_URL_DEV` empty. The callers must be set **before the first deploy**: an empty caller set on Cloud
   Run refuses the revision.
4. **Pass one: `make deploy`.** The service starts with `SANDBOX_URL` empty, serves **only `/health`**, and answers every
   op with `503 unconfigured`.
5. **Read the URL back** and put it in `.env` as `SANDBOX_RENDER_URL_DEV=<https:// URL>`:
   ```bash
   gcloud run services describe alek-bot-sandbox-render-dev --region=us-central1 --format='value(status.url)'
   ```
6. **Grant invoker to the main runtime only:** `roles/run.invoker` on the render service to `<main-runtime-sa>`; never
   `allUsers` or `allAuthenticatedUsers`. Audit once and after each deploy that neither the project nor the service policy
   contains them.
7. **Pass two: `make deploy`.** The render service now knows its audience; the main service has the URL. DOCX and PDF now
   render through the service. With an empty URL the main service still starts (ERROR logged) and DOCX and PDF report the
   sandbox unavailable, on Cloud Run and locally.

While `SANDBOX_RENDER_SA_DEV` is empty, `cloudbuild_sandbox_render.sh` exits 0 in every mode, so `make deploy` works before
step 1.

### Sandbox light (parse) service

`alek-bot-sandbox-light-dev` parses uploaded and drive files for the main service and the indexer
([`../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md`](../10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md) §4, §11 light row). It is
`sandbox_main.py` in the same image file as the render service (`Dockerfile.sandbox`, target `light`; an explicit COPY
allowlist, no `.env`, no key file), `--no-allow-unauthenticated`, `--cpu-throttling` (request-based billing: idle warm
instances cost nothing), an in-memory volume at `/work` (sized in `scripts/infrastructure/cloudbuild_sandbox_light.sh`
for the largest request body, response windows and child file; changed by editing that script). The instance stays warm
after a normal request and is recycled only after an abnormal one (`SANDBOX_RECYCLE=abnormal`, light only), and its
supervisor closes `/tmp` and `/dev/shm` to the engine uid at start-up, refusing to start when that does not hold
(`decisions/sandbox_render_service.md`). Both runtimes parse only through it: the deploy target refuses an empty
`SANDBOX_LIGHT_URL_DEV`, and the main service and the indexer refuse to start without `SANDBOX_PARSE_URL` (set from
`_SANDBOX_LIGHT_URL`; `src/composition/sandbox_parse_setup.py::build_sandbox_parser`), locally too. Deploy the service
(steps below) before the first deploy that switches the callers.

The `light` image also carries `ffmpeg` (the `render` target does not) for the `media.*` ops. Those are tunnel ops on
`POST /v1/media.<op>` (the indexer opens a full-duplex HTTP/2 stream and serves byte ranges of the recording, so the
service needs no egress and receives no URL or credential). The caller map gives both the **indexer** and the main service
the prefixes `parse` and `media`. The indexer needs no extra env key: the media adapter targets the same
`SANDBOX_PARSE_URL` (no `SANDBOX_MEDIA` knob).

| `.env` key | Substitution | Meaning |
|---|---|---|
| `SANDBOX_LIGHT_SA_DEV` | `_SANDBOX_LIGHT_SA` | the light service's runtime identity; **empty skips every light build step** |
| `SANDBOX_LIGHT_URL_DEV` | `_SANDBOX_LIGHT_URL` | the light service's own `https://` URL: the callers' target and the service's token audience (`SANDBOX_URL`) |
| `SANDBOX_LIGHT_CALLERS_MAIN_DEV` | `_SANDBOX_LIGHT_CALLERS_MAIN` | emails admitted as the main service (`:`-separated; a comma is refused) |
| `SANDBOX_LIGHT_CALLERS_INDEXER_DEV` | `_SANDBOX_LIGHT_CALLERS_INDEXER` | emails admitted as the indexer (`:`-separated; a comma is refused) |

**Owner steps, in order** (nothing here is run by an agent):
1. **Create the identity and grant it nothing.** Its name must start with `alek-sandbox-`: the deploy script refuses any
   other `SANDBOX_LIGHT_SA_DEV` with exit 3. Verify that
   `gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings[].members --filter="bindings.members:<sandbox-light-sa>"`
   prints nothing.
2. **Let the deployer act as it:** `roles/iam.serviceAccountUser` on `<sandbox-light-sa>` (that account only) for the
   build's deployer identity.
3. **Put the keys in `.env`:** `SANDBOX_LIGHT_SA_DEV=<sandbox-light-sa>`, `SANDBOX_LIGHT_CALLERS_MAIN_DEV=<main-runtime-sa>`
   and `SANDBOX_LIGHT_CALLERS_INDEXER_DEV=<indexer-runtime-sa>`. The callers must be set **before the first deploy**: an
   empty caller set on Cloud Run refuses the revision. `SANDBOX_LIGHT_URL_DEV` is required by the deploy target itself:
   for pass one set it to the service's expected `https://` URL (Cloud Run derives it from the service name and project
   number).
4. **Pass one: `make deploy`.** The service starts with `SANDBOX_URL` set to the expected URL from step 3, so it is
   configured at once; the callers' invoker grant (step 6) is still missing, so their calls are refused until then.
5. **Read the URL back** and check it equals `SANDBOX_LIGHT_URL_DEV`; if it differs, correct the key:
   ```bash
   gcloud run services describe alek-bot-sandbox-light-dev --region=us-central1 --format='value(status.url)'
   ```
6. **Grant invoker to the two callers only:** `roles/run.invoker` on the light service to `<main-runtime-sa>` and
   `<indexer-runtime-sa>`; never `allUsers` or `allAuthenticatedUsers`. Audit once and after each deploy that neither the
   project nor the service policy contains them.
7. **Pass two (only if the URL had to be corrected): `make deploy`.** The service and its callers now agree on the audience.

While `SANDBOX_LIGHT_SA_DEV` is empty, `cloudbuild_sandbox_light.sh` exits 0 in every mode.

**Rollback of the parse switch.** Roll back both callers to their previous revision (`update-traffic --to-revisions=<prev>=100`
on the main service and on the indexer), then revert the change. Traffic pinned to a revision needs `--to-latest` later.

---

## The drive specialist (`work_with_drive_files`)

(`docs/10_rfcs/DRIVE_SPECIALIST_RFC.md` §10, §13; `decisions/drive_specialist.md`; several drives:
`docs/10_rfcs/MULTI_DRIVE_RFC.md`.) It adds no service, secret, Firestore index or Cloud Build substitution: it runs in the
main service on keys it already has, so the ordinary `make deploy` is the whole code change. Its prompt lives in Firestore
like every other agent's (tokens `COGNITIVE_PROCESS_DRIVE`, `PROTOCOL_DRIVE_SEARCH`, `PROTOCOL_DRIVE_NOTES`,
`OUTPUT_FORMAT_DRIVE`, blueprint `drive_agent_v1`, profile `drive`) and ships by upload, not by deploy. There is no fallback
prompt: a missing `drive` profile fails every commission with "PromptBuilder failed" (`preload_cache("drive")` only warns
at startup). The owner uploads the prompt layer before the release whose code needs it ("Every deploy" below), and
`PROTOCOL_AGENT_SELECTION`, which names `work_with_drive_files`, goes up right before that release, not earlier. A drive
connection without `write_folder_id` reads as the drive's root; a connection created now starts read-only until the owner
picks a write folder in the Cabinet.

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
  caches a template per instance, so fresh instances must find the new token.
- **`--set-env-vars` replaces every variable.** A variable set by hand with `gcloud run services update` is gone at the
  next deploy; a lasting setting goes into `cloudbuild-dev.yaml` or a deploy-only `.env` key.
- **Env drift check.** `make deploy` ends with `scripts/infrastructure/check_env_drift.py`: for each service the build
  deploys with `gcloud run deploy`, it compares the variable names of the newest revision with the one before it and
  prints every name that disappeared (names only, never values), exiting nonzero. The deploy has already happened;
  re-set the variable or add it to `cloudbuild-dev.yaml`.
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

**Image registry retention.** `python scripts/infrastructure/artifact_registry_retention.py` (dry run; `--apply` sets the
cleanup policy and cleans the backlog) deletes versions older than the policy's age that are not among the newest kept per package; it refuses when
an image a revision or job needs would go (`decisions/artifact_registry_retention.md`).

**The `:latest` trap.** After a failed deploy or a traffic rollback, `:latest` still points at the new image. Every
`gcloud run services update`, and every Makefile target that wraps it (`start`/`stop`, the `claude-`/`openai-`/`dr-`
rollback and forward switches), creates a new revision from that image; with traffic pinned to the old revision it gets
0 %, so a kill-switch silently does nothing, and a later `--to-latest` serves the bad image. Run no `services update` and
no Makefile switch until the fix is deployed forward; if one is unavoidable, follow it with an explicit `update-traffic`.

**Per-service knobs** (optional; each read with `os.getenv` at its call site, never in `load_settings()`, so none has a
deploy key unless stated; set one by hand only for a test, a lasting value goes into `cloudbuild-dev.yaml`).
- **Model pins (Claude ids only; empty = the agent's default).** `FACT_PROFILER_MODEL`
  (`src/composition/fact_profiler_runner.py::profiler_model`, every account), `FACT_QUERY_SLOTS_MODEL` (the memory-search
  slots call; `src/composition/fact_query_slots_setup.py::search_slots_model`; the router's slots call is not affected)
  and `EMAIL_CARD_MODEL` (`src/composition/email_card_setup.py`). Their strategies have no other provider and no fallback.
- **Fact profile step cap.** `FACT_PROFILER_MAX_PER_RUN` (`src/composition/fact_profile_setup.py::max_per_run`): the most
  model calls one profile step makes in a leased run; `0` = re-derivation only, no spend; an unparsable or negative value
  is the default with one WARNING.
- **Email card online step.** `EMAIL_CARD_ONLINE` (code default off; `1` = on) makes the main service's email indexing also card
  each new kept mail. `make deploy` sets it on the main service from `_EMAIL_CARD_ONLINE` (default `1`), fed by the deploy-only key
  `EMAIL_CARD_ONLINE_DEV` (`0` turns it off across deploys). With it off, kept mail gets no card and memory sees its
  headers only. `EMAIL_READER_PAGES_PER_DAY` (`src/domain/email_attachment_policy.py::EMAIL_READER_PAGES_PER_DAY_DEFAULT`;
  `0` = the online scan reader off) caps the daily pages the card step may hand to the paid reader; it has no deploy key.
- **Email card search and memory feed have no switch:** `search_emails` always answers from the cards and consolidation's
  email stage always reads mail with its card (`docs/05_building_blocks/memory/README.md` Step 8). Upload before the
  deploy (`firestore_utils/snapshot_upload.py --apply`; none of these agents has a fallback prompt): the card prompts
  (`COGNITIVE_PROCESS_EMAIL_CARD`, `OUTPUT_FORMAT_EMAIL_CARD`, blueprint `email_card_agent_v1`, profile `email_card`), the
  slots prompt (`OUTPUT_FORMAT_EMAIL_QUERY_SLOTS`, `email_query_slots_v1`, `email_query_slots`; without it every search runs
  without slots, one `[EmailQuerySlots] outcome=degraded` line each) and `CONSOLIDATION_EMAIL_CANDIDATES`
  (`consolidation_email`, `consolidation_email_v1`; without it every Stage 3 run fails with `Stage 3 (email) failed`, the
  mail stays unconsolidated, nothing is lost). Indexes: `make deploy-indexes` skips vector indexes, so the card vector
  index is created by `scripts/infrastructure/create_email_card_vector_index.sh` (without it the meaning list is skipped
  with one WARNING per search and only the key lists answer); the consolidation composite comes with `make deploy-indexes`.
- **Memory telemetry.** `MEMORY_TELEMETRY=off` and `MEMORY_SAMPLE_INTERVAL_S` (§ Memory telemetry sink above).

**Drive indexing.**
- Pause / resume the drive work: `gcloud tasks queues pause|resume drive-sync-dev --location=us-central1 --project=<PROJECT_ID>`
  and `gcloud scheduler jobs pause|resume alek-bot-dev-start-drive-sync --location=us-central1 --project=<PROJECT_ID>`. A
  running tick keeps going until its deadline after a pause.
- Stop every upgrade pass: `gcloud run services update alek-bot-indexer-dev --update-env-vars DRIVE_UPGRADE_PASS=off
  --region=us-central1 --project=<PROJECT_ID>`. **It does not survive a deploy** (`--set-env-vars`): set it again after each
  deploy while it is needed. The durable form is `DRIVE_UPGRADE_PASS_DEV=off` in `.env` and a deploy. Rollout rule for a
  parser version bump: `.env` off before the deploy, the deploy (clients before the sandbox), the owner runs the read-only
  probe and OKs its number, then `DRIVE_UPGRADE_PASS_DEV=on` and a deploy.
- Stop model spend only: remove `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the indexer's secrets; every model call is then
  a model outage (never charged to a file), and parsing, cards and search go on. The Gemini fallback does not take over
  (`GEMINI_API_KEY` stays for embeddings); to turn the fallback off, set `"fallback": None` in the drive entries of
  `AgentProviderStrategy.STRATEGIES` (`src/services/agent_context_builder.py`).
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
- Transcription: the connection's flag off (`scripts/infrastructure/drive_transcribe_flag.py --user USER [--connection ID] --off --apply`) stops
  that connection's new work and the mirror folder; the kill switch off (`DRIVE_TRANSCRIBE_ENABLED_DEV=off`, redeploy) stops it
  for everyone and freezes in-flight batches. There is no mirror-only switch.

**Drive notes** (`docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md` §3.6). Stop every note write: `DRIVE_NOTES_ENABLED_DEV=off` in `.env`
and `make deploy` (it echoes `_DRIVE_NOTES_ENABLED=` before it submits). One substitution sets the main service and the
indexer together: no note tool for the drive specialist, the `$drive-notes` commands answer "not available", and the
indexer does no notes work. Reads go on (the open header, the deleted-files search, copies already in cards). Never use
the local name `DRIVE_NOTES_ENABLED` for the deploy, and never set it by hand with `services update` (`--set-env-vars`
replaces it at the next deploy).

**Sandbox render.** There is no in-process path to go back to: roll the main service back to a previous Cloud Run revision
and revert the removal commit; the render service can stay up unused. **Hard stop:** remove the caller's `roles/run.invoker`
binding on the render service. Never empty `SANDBOX_RENDER_CALLERS_DEV` to stop it: an empty caller list refuses the new
revision, so the old one keeps serving.

**Agent workspace erasure** (no user-facing path; owner command, removes every generation: soft delete is off, so nothing
remains): `gcloud storage rm --all-versions "gs://<WORKSPACE_BUCKET>/<USER_ID>/**"`.

**Fact janitor.** Stop it at once: `gcloud scheduler jobs pause alek-bot-dev-fact-lifecycle-sweep --location=us-central1
--project=<PROJECT_ID>`. It writes because the main service's `--set-env-vars` carries `FACT_LIFECYCLE_SWEEP_DRY_RUN=0`
(`src/composition/memory_setup.py`); the rollback lever is removing that variable from `cloudbuild-dev.yaml` and deploying,
which makes it log-only again ([memory map](../05_building_blocks/memory/README.md) §5.5; [SCHEDULERS.md](SCHEDULERS.md)
§ Fact Lifecycle Sweep).

---

## Platform limits the design works around

External limits, not our constants (the constants live in code):

- **Cloud Run request timeout:** at most 60 minutes per request; longer work goes to Cloud Tasks or a Cloud Run Job.
- **Cloud Run filesystem:** in-memory and per instance, no persistent disk; all state is in Firestore or GCS.
- **Firestore document size:** at most 1 MiB; a session or fact set that can grow is split into several documents.

## Operations

**Status and logs.** `gcloud run services describe <service> --region=us-central1`, `gcloud scheduler jobs list --location=us-central1`,
and the `make logs*` / `fetch-logs*` targets. Log queries and the Logfire cascade: [LOGGING.md](LOGGING.md). Alert policies:
[ALERTING.md](ALERTING.md).

**Keep-alive and image cleanup.** Scale-to-zero costs cold starts; a keep-alive ping costs cents a month; `min-instances=1`
costs a real monthly amount. The keep-alive scheduler job is created by the deploy build itself (`scheduler-jobs` step);
no setup script is needed: [KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md). Every deploy moves `:latest` and leaves the
previous digest untagged; the standing cleanup is the retention policy under Standing levers. `scripts/infra/` holds the
older manual cleaner for the `gcr.io` repository (`cleanup_gcr_images.py`, `gcr-io-cleanup-policy.json`), superseded by it.

---

## Related guides

- [SCHEDULERS.md](SCHEDULERS.md) — every Cloud Scheduler job and task type: schedule, payload, deadlines
- [KEEP_ALIVE_SETUP.md](KEEP_ALIVE_SETUP.md) — keep-alive pings against scale-to-zero
- [LOGGING.md](LOGGING.md) — structured logging, Cloud Logging queries, Logfire
- [ALERTING.md](ALERTING.md) — alert policies and logging exclusions
- [../../BOOTSTRAP.md](../../BOOTSTRAP.md) — from-scratch setup
