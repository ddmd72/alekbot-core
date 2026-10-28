# Alek-Core

Personal exocortex — a knowledge management system powered by LLMs.
Solo developer. Production on GCP (Cloud Run + Firestore).

## Dev User IDs

When you need user_id / account_id for manual triggers, gcloud commands, or scripts — read from memory (`project_infra.md`). Never ask the user for these values.

## Manual Triggers (dev)

`/worker` verifies a Google OIDC token (see
`docs/04_solution_strategy/decisions/worker_oidc_and_docx_sandbox.md`), so a plain unauthenticated
`curl` returns **401** against the live service. Project ID / user IDs / SA: read from `.env` /
memory (`project_infra.md`) — don't hardcode. Three ways to trigger, easiest first:

**1. Scheduled task types — run the Cloud Scheduler job** (it already carries OIDC; nothing to mint; `cloudbuild-dev.yaml` creates every job with the invoker identity read from the `SERVICE_ACCOUNT_EMAIL` secret):
```bash
gcloud scheduler jobs list --location=us-central1 --project=<PROJECT_ID>   # see all jobs
gcloud scheduler jobs run alek-bot-dev-fire-due-reminders --location=us-central1 --project=<PROJECT_ID>
```

**2. Arbitrary per-user payload against the live service — mint an OIDC token for the worker SA.**
Needs a one-time grant of `roles/iam.serviceAccountTokenCreator` on the SA to your user.
`--include-email` is **mandatory** — the verifier checks the token's `email` claim (audience is not
pinned, so its value is irrelevant but the gcloud flag is required):
```bash
SA=$(gcloud secrets versions access latest --secret=SERVICE_ACCOUNT_EMAIL --project=<PROJECT_ID>)
# one-time grant (you choose to run this — it widens your access to the SA):
# gcloud iam service-accounts add-iam-policy-binding "$SA" \
#   --member="user:$(gcloud config get-value account)" \
#   --role=roles/iam.serviceAccountTokenCreator --project=<PROJECT_ID>
TOKEN=$(gcloud auth print-identity-token --impersonate-service-account="$SA" \
  --audiences="$SERVICE_URL_DEV/worker" --include-email)
curl -s -X POST "$SERVICE_URL_DEV/worker" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"task_type": "daily_email_review", "user_id": "<DEV_USER_ID>", "account_id": "<DEV_ACCOUNT_ID>"}'
```

For `drive_sync` (indexer, not `/worker` of the main service) go through the queue instead:
`gcloud tasks create-http-task --queue=drive-sync-dev … --oidc-service-account-email=<DRIVE_INVOKER_SA_EMAIL or SERVICE_ACCOUNT_EMAIL value>`
(needs actAs on that identity, not tokenCreator; details in `docs/07_deployment/SCHEDULERS.md`).

**3. Local instance — the gate bypasses** when no OIDC identity is configured and `K_SERVICE` is unset (local dev; on Cloud Run an empty set refuses every call), so a
plain unauthenticated `curl http://localhost:8080/worker ...` works with no token.

## Commands

```bash
make check             # Local pre-push gate (once per push, not per commit): ruff lint + mypy --strict (domain/ports) + unit/architecture tests, no slow tests, no coverage
make typecheck         # mypy --strict on src/domain + src/ports only
make check-types       # mypy (lenient) on src/adapters — informational, prints an error count, never fails
make test              # All tests (the unit/architecture suite excludes `slow` tests; they run in `make ci`)
make test-unit         # Unit tests
make test-affected     # Inner loop: only the unit tests testmon selects for your changes (seconds on a small diff)
make ci                # PR job (what CI runs): lint + typecheck + the unit suite once with slow tests, coverage gate + offline G2
make test-integration  # Integration tests
make test-e2e-all      # E2E all agents
make deploy            # Build + deploy to Cloud Run (single live environment)
make logs              # Recent Cloud Run logs
make logs-tail         # Live tail logs
make fetch-logs [K=300] # Pull last K logs to alek_debug.log (grep locally — see Debugging below)
```

**Which test command to run when** (while editing, before a commit, before a push, before calling work done): the table
in `tests/CLAUDE.md` § Running is the one place that says it.

Lint + format via `ruff` (`make lint` / `make format`, config in `ruff.toml`). `make check` runs
`ruff check src/ main.py indexer_main.py job_main.py relay_main.py sandbox_main.py` and `mypy --strict` on `src/domain` + `src/ports` (`mypy.ini`; adapters stay
informational, `mypy-adapters.ini`, see `decisions/mypy_scoped_rollout.md`) before the unit suite (which excludes `slow` tests; they run in `make ci`); it is the local pre-push gate, run once per push (intermediate commits use `make test-affected` plus the tests of any data folder they touch). CI (`.github/workflows/ci.yml`) runs
`make ci` (the same plus slow tests and the coverage gate) on every push/PR (posting ✅/❌ to Slack). Docs-only PRs skip it (also a push to `main` and the `timing` job) but still run the doc-reading tests listed in `scripts/ci/docs_only.py` (`DOC_READING_TESTS`; that file defines "docs": every file under `docs/`, root `*.md`, `.github/**/*.md`, `CLAUDE.md` anywhere except under `src/skills/`). Two weekly cloud workflows, `Mutation survivors` and `Firestore parity`, write artifacts that a scheduled routine reads, never Slack (`tests/CLAUDE.md` § Running). The full unit/architecture suite (~13 300 tests) is green
and **safe to run in full** (any historical "don't run the full suite" warning was a since-fixed
chunker infinite loop, not a standing hazard). `make test-unit` / `make check` run it in parallel
via pytest-xdist (`-n auto`; cap with `PYTEST_WORKERS=6`): ~1:34 on a 12-core Mac vs ~5:00
single-process. The `-n` flag lives in the Makefile, not `pytest.ini`, so a targeted
`pytest path::test` stays one process; integration/e2e targets stay serial. A test that fails only
under xdist is order-dependent — a real test bug, fixed under the reviewer rule, not re-serialised. **Timing
tests are off the everyday gate (owner decision, 2026-10-20).** Any test that asserts on wall-clock or CPU time
carries `@pytest.mark.timing`; `tests/conftest.py` skips it unless `RUN_TIMING_TESTS=1`, so `make check`,
`make test-unit` and a plain `pytest` never run it. They failed on a loaded machine with no code change, and
every full run then failed and was re-run. They run only via `make test-timing`: in CI as the separate `timing`
job after a merge to `main` (never on a PR), or by hand. A new test with a time threshold gets the marker;
never put timing tests back into `make check`, and never re-run a suite because a timing test failed.
**Slow tests and the inner loop (P1, 2026-10-22).** `@pytest.mark.slow` mirrors `timing`: a test over 0.5 s with
coverage on (setup + call + teardown) is skipped unless `RUN_SLOW_TESTS=1`, which `make ci` sets; so `make check` stays fast
and `make ci` runs everything once. Locally `--durations=25` is an advisory report. `make test-affected` runs
`pytest --testmon` once, seeds `.testmondata` from the main checkout in a new worktree, and never combines testmon with
coverage; it selects by executed Python lines, so it can miss a test whose input is a data file or code run in a child
process: an intermediate commit adds the tests of any data folder it touched, and **`make check` once before a push is mandatory** (`tests/CLAUDE.md`). **One full suite at a time on a
machine:** a session with `-n` (or 2000+ tests) takes `fcntl.flock` on a file in the temp dir, prints the holder and
waits up to `PYTEST_LOCK_TIMEOUT_S` (1800); `PYTEST_SUITE_LOCK=0` turns it off. **Test rules (P2, 2026-10-22), in `tests/CLAUDE.md`:** fakes with semantics over call assertions; `tests/unit/` is offline (a network guard refuses non-loopback `connect()`, a tripwire refuses `google.auth.default` and non-local gRPC channels); `make ci` enforces per-layer branch-coverage floors that rise, or are lowered per the rule in `tests/CLAUDE.md` (reason line + reviewer OK) (`scripts/check_coverage_layers.py`) and a shrinking allowlist of src modules no test imports; mutation testing is the weekly cloud run only (`Mutation survivors`, owner decision 2026-10-27): a package registers its modules in `tests/support/mutation_targets.py` and runs no local mutation pass (`tests/CLAUDE.md`). Lint scope is `src/` and the root entry points (default high-signal ruleset: pyflakes + pycodestyle
errors). `ruff format` exists as a dev convenience but is not enforced in CI — the codebase is not
mass-reformatted.

## Code Navigation (CodeGraph)

A local code knowledge graph is available via the `codegraph` MCP server (index in `.codegraph/`,
gitignored, ~28MB, machine-local — run `codegraph init` once per clone; the server may be absent in
some environments — fall back to grep/Read when its tools aren't listed).

**Default to `codegraph_explore` FIRST** for "how does X work / where is X / trace this flow /
survey this area" — it returns the verbatim source of the relevant symbols in one call
(Read-equivalent). Use grep/Read only to confirm a detail it didn't cover. This cuts the usual
grep+Read fan-out to 1–2 calls.

**Catches well (trust it):**
- Whole-area / flow source in one shot (e.g. the DelegationEngine dispatch chain, an adapter's
  request build) — verbatim, well-targeted.
- Static structure: `extends` (port→adapter, agents→BaseAgent), `codegraph_callers`/`callees`,
  `codegraph_impact` (symbol + its tests).

**Dive deeper yourself (it stops at the doorstep):**
- **Runtime dispatch** — intent→agent via `agent_manifest.py`/`agent_registry.py`, provider/tier
  via `AgentProviderStrategy`, DI wiring in `composition/`. Explore points you to
  `handle_delegation` etc. but does NOT resolve the registry/config mapping — grep those files.
- **Class-level constants / config dicts** — `codegraph_search` returns name-substring test
  matches, not the `FOO = {...}` definition. Grep instead.
- The relationships "callers" list is polluted with `scripts/`+`tests/` noise — trust the Source
  section, filter the rest. `codegraph affected` (source→test mapping) is unreliable — use
  `make test-unit` / grep to find affected tests.

**Keeping it fresh:** a native file watcher re-indexes ~1–2s after edits (debounced), tool
responses warn when referencing pending files, and the MCP server reconciles against the working
tree on reconnect — so normal editing stays fresh automatically. After a big branch switch / rebase
/ bulk change made while the server was down, run `codegraph sync` (incremental) or `codegraph
index` (full); `codegraph status` shows index health.

## Branching & Environment

- **Single live environment.** The separate prod deployment was retired (2026-05-31); the
  `development_`-prefixed collections in the `us-production` database are the sole live data.
  The prod-suffix path and the env-prefix mechanism are kept by decision (a future real prod
  would reuse them) — collections are NOT renamed to drop the prefix (Firestore has no native
  rename; migration not worth it). See
  `docs/04_solution_strategy/decisions/collection_prefix_retained.md` and
  `docs/04_solution_strategy/decisions/dead_prod_collections_deletion.md`.
- **Default branch is `main`** (renamed from `develop` 2026-06-01; the old prod-tracking `main`
  was deleted). One trunk.
- **Branch discipline:** every change reaches `main` through a pull request — a GitHub ruleset on
  `main` (since 2026-10-22) refuses direct pushes, force pushes and deletion for everyone, owner
  included, with no bypass: cloud sessions push with the owner's own token, so a bypass for him
  would be a bypass for them. Small isolated changes get a small PR. Every change keeps the
  affected docs (this file, arc42 in `docs/`, decision records) in sync. Documentation drift is
  the thing branch discipline exists to prevent.
- **Deploy-substitution trap:** `make deploy` does `include .env` and passes some keys as cloudbuild
  `--substitutions`, so any `.env` key reused that way bakes its **local** value into the deployed
  revision (this leaked `localhost` into prod `OAUTH_REDIRECT_URI`). When a value differs between
  local and deploy, give the deploy side its own key (e.g. `SERVICE_URL_DEV`) — never reuse the
  dual-purpose local key. Deploy reads the **working tree, not git** — uncommitted changes still ship.
- **`config.get()` vs `os.getenv()` — an env var read the wrong way is silently dead.** The `config`
  dict from `load_settings()` lists its keys by hand; `config.get("ANYTHING_NOT_LISTED")` returns
  `None` with no error, which is how `OPENAI_DEEP_RESEARCH_MODEL`, `CLAUDE_DEEP_RESEARCH_MODEL` and
  `SLACK_BOT_USER_ID` were dead from the day they were written (fixed 2026-08-16). Rule: **secrets and
  required configuration go in `load_settings()`; optional knobs — model pins, rollback switches — are
  read with `os.getenv()` at their call site.** Registering an optional knob is not free either: every
  empty key in that dict is chased into Secret Manager on each boot.
  The sandbox knobs are of this kind: `SANDBOX_RENDER`, `SANDBOX_RENDER_URL` (main service); `SANDBOX_URL`,
  `SANDBOX_CALLERS_RENDER`, `SANDBOX_RECYCLE`, `SANDBOX_WORK_ROOT`, `SANDBOX_OUTPUT_ROOT`, `SANDBOX_ENGINE_USER` (render service).
  So is `FACT_LIFECYCLE_SWEEP_DRY_RUN` (main service): the fact janitor is log-only unless it is an explicit off value
  (`0`, `false`, `no`, `off`); the deploy sets it to `0` (a literal in `cloudbuild-dev.yaml`), so the janitor has written since the
  deploy of 2026-10-27. Rollback: remove the variable from that `--set-env-vars` (a deploy; `--set-env-vars` replaces every variable).

## What and Why

Exocortex — AI extension of memory and thinking via Slack/Telegram.
Not a chatbot. A system that remembers, thinks in the background, and responds to the point.

**Cycle:** user speaks → bot responds using accumulated knowledge →
background process extracts new facts from the conversation → bot gets smarter.

## Key Mechanisms

**Multi-agent network** — specialists, not one LLM for everything. Tiers: ECO/BALANCED/PERFORMANCE.
Full per-agent detail (mechanics, intents, tiers, gotchas) lives in
[`src/agents/CLAUDE.md`](src/agents/CLAUDE.md) → Agent Roster. Quick index:

| Agent | Tier | Key intents | One-liner |
|-------|------|-------------|-----------|
| Router (Gemini) | ECO | — | LLM triage every request; **always routes to Smart** |
| Smart | per-config | `delegate_to_specialist` | primary path; re-evaluates after tool results |
| Quick | ECO | — | emergency fallback + `notify` formatter (never primary) |
| WebSearch | BALANCED / **ECO on `fetch_url`** | `search_web`, `fetch_url` | provider-native grounded search |
| Memory | ECO | `search_memory` | multi-vector RRF; search only. Facts from a document: Smart's local tool `propose_memory_facts` + the owner's `$memory save` (below) |
| EmailSearch | ECO | `search_emails`, `get_email_details`, `get_email_attachment` | |
| EmailClassification | BALANCED | — | called by EmailIndexingService, not by agents |
| DocPlanner | PERFORMANCE | `create_document` | JSON layout spec → DocGenerator |
| DocGenerator | BALANCED | `generate_docx_code` (internal) | Node.js script → DOCX |
| PdfGenerator | BALANCED | `create_pdf` | HTML+CSS → Puppeteer → PDF |
| HtmlPageGenerator | PERFORMANCE (**Grok**) | `create_html_page` | full HTML+CSS+JS; Unsplash placeholders |
| FileManagement | zero-LLM | `open_file`, `delete_file` | chat uploads, delivered documents, `skill:` / `workspace:` refs; opens a `drive:` ref leniently |
| Drive (`DriveAgent`) | BALANCED (Claude `claude-sonnet-5-5`, adaptive thinking medium; fallback OpenAI `gpt-6.1-sol` only when Claude is the primary; a global `provider_preference: openai` moves it to the rejected `gpt-6-luna`, so pin `agent_providers["drive"]="claude"`) | `work_with_drive_files` | the user's drive: one commission, local CRUD tools over the user's `DriveOperations`; every read reaches Smart verbatim; the owner's file notes (branch) |
| Notes / Self-Reminders | PERFORMANCE (OpenAI) | `manage_self_reminders` | autonomous deferred firing; optional code-bound `workspace_file` |
| Tasks | — | `manage_user_tasks` | MicrosoftToDo (GoogleTasks frozen) |
| Consolidation | PERFORMANCE (Claude) | — | background memory formation (see below) |
| DeepResearch | — | `deep_research` | async; default Claude, Cloud Run Job; opt-in HTML copy to the user's drive |
| MapsSearch | BALANCED (OpenAI) | `maps_query` (internal) | auto fan-out from `search_web` |
| Compute | ECO | `compute_*` | Gemini `code_execution` sandbox, compute-only |
| ImageGeneration | ECO default (**Grok**-only) | `generate_image`, `edit_image` | grok-imagine-image-2.0 (Aurora) via `ImageGenerationPort`; ASYNC, delivers as document |
| Tutor | BALANCED (OpenAI `gpt-6-luna`) | `tutor_chat` | bound-channel-only companion; text language tutor, session-scoped memory (RFC `COMPANION_AGENTS_RFC.md`, roster detail in `src/agents/CLAUDE.md`) |
| Lelik | n/a — session is OpenAI Realtime in the relay | internal; allowlist `search_memory`, `search_web_light`, `ask_alek`, `tell_alek` | voice companion: callback, session config, delegation via `/voice/delegate` |
| Alek gateway | zero-LLM | `ask_alek` (SYNC), `tell_alek` (ASYNC errand) — internal | Lelik's route to Router → Smart; question: links copy to chat; errand: full outcome to chat |
| WebSearchLight | ECO (Gemini flash-lite) | `search_web_light` (internal, Lelik only) | one grounded lookup, ~2 s, spoken answer |
| lelik_summarizer | ECO | none (not manifest-registered) | end-of-call transcript → plain-text summary; run by `CompanionExtractorRunner`, same slot `tutor_extractor` fills; no provider fallback by decision (a lost note is alerted, `src/agents/CLAUDE.md`) |
| DriveDocumentReader | ECO (OpenAI `gpt-6-luna`, or Claude by `agent_providers`; fallback Gemini `gemini-3.8-flash`) | none (indexer only) | PDF window or image → verbatim text + brief; `ContentExtractorPort` via `DriveReaderRunner` |
| DriveImageTriage | ECO (same switch and fallback) | none (indexer only) | 800 px thumbnail batch of 8 → text or photo + description, objects, scene, visible text (triage v2); `ImageTriagePort` via `DriveTriageRunner` |
| DriveProfiler | ECO (same switch and fallback) | none (indexer only) | one text file's stored text → profile (type, life areas, dates, entities, values, topics, en/es/uk/ru word lists), verified against the text; `DocumentProfilerPort` via `DriveProfilerRunner` |
| DrivePassages | ECO (same switch and fallback) | none (indexer only) | 2 chunks → keywords and questions with evidence quotes (doc2query), grounded in the chunk; `PassageHintPort` via `DrivePassageRunner` |
| DriveTranscriber | Gemini `gemini-3.8-flash` on the **Batch API** (BALANCED, no provider switch, never flash-lite) | none (indexer only; not a `_call_llm` agent) | one ≈ 30-min audio window → `{language_codes, speakers, no_speech, segments[{start MM:SS, speaker, text}]}`; `BatchTranscriptionPort` via `DriveTranscriptRunner`; built only with the kill switch on; **T1 live (owner, 2026-10-21)** |

**Long-running turns** — one absolute 25-min clock per chat turn (`domain/turn_clock.py`,
`TurnClock` in a `ContextVar`; `use_turn_clock=True` only on Smart's top-level engine). At 90 s
unanswered: a status note + `[user, notice]` pair, so a second message sent meanwhile is answered
normally. Same-provider retries and cross-provider failover clamp to the remaining clock (none in
the wrap-up reserve); a tool batch is cut at the budget. The clocked orchestrator never gets a
whole-turn retry, before or after the mark (plan delta D2). `LongTurnService` (registry +
heartbeat + cancel, `retry_store_write`) over `FirestoreLongTurnRegistry` (`{prefix}long_turns`,
record created at the mark) persists it. A finished run posts a `[late answer]` + permalink to the
main feed (Slack: top-level, incl. overflow) and appends a `[System: late answer to …]` pair to
history; a failed, cancelled or lost turn closes its question with the same pair. The wrap-up and
chat-since notes live only in the run's own transcript, never in history. A retry of a turn that
is `running` but not live in this process is reported lost. Telegram updates go via `/worker`
`task_type=telegram_update` (a named Cloud Task: the update id is its dedup), not inline; Slack's lock releases at the mark.
Smart sees its running jobs and can cancel one, per `PROTOCOL_LONG_TURNS`. The two-phase timeout
path is retired. Not in scope: `ask_alek`/`tell_alek`/notifications stay unclocked (RFC §7
deferred; see `decisions/long_running_turns.md`).

**Public landing** — `/` serves `src/web/static/landing.html` to visitors without a session (it is the
GitHub repo's homepage); a signed-in owner is still redirected to `/cabinet`. `src/web/landing_app.py`
also serves `/og.png` (share image) and `/robots.txt` (`Allow: /$`, everything else closed). The page is
one self-contained file, no external requests; its numbers (tests, RFCs) are hand-maintained.

**Access control: one whitelist, two gates.** `{prefix}domain_whitelist_v1/config` (emails + domains)
decides who gets in. Chat: `IAMService.authorize` on every Slack/Telegram message. Cabinet:
`AuthenticationService._require_allowed_email` on every Google sign-in, new and existing users,
before any user lookup (verified ID-token email, `email_verified` required). Until 2026-10-09 the
Cabinet path had no gate, so any Google account could register. A revoked email keeps Cabinet access
until its session ends, at most 24h. See `decisions/cabinet_oauth_whitelist_gate.md`.

**Remote MCP Server** — alekbot as MCP *server* exposing memory search to claude.ai Custom Connectors
(inverse of its Maps MCP *client*). One tool `get_user_context(query, …)` → `SearchEnrichmentService.enrich_context`
directly (bypasses the agent stack). Full in-process OAuth 2.1 AS (DCR, PKCE S256, RFC 8707, refresh
rotation) via the `mcp` SDK; endpoints at server root routed by a plain ASGI dispatcher in `main.py`
(NOT Starlette `Mount`). SDK shim `composition/mcp_sdk_oauth_provider.py` in `composition/` (REQ-ARCH-01).
**MVP, dev-only, experimental.** Code under `src/{domain/mcp.py,ports/mcp_client_repository.py,adapters/firestore_mcp_client_repository.py,services/mcp_authorization_service.py,composition/mcp_*.py,web/mcp_consent_app.py}`.
See `docs/05_building_blocks/remote_mcp_server/` + `docs/10_rfcs/REMOTE_MCP_SERVER_RFC.md`.
- **Tool schema: accept liberally, advertise strictly** (fixed 2026-09-18). FastMCP derives the wire
  schema from type hints, so `Optional[T]` emits `anyOf:[T,null]` — clients collapse the union and
  guess the argument shape. Optionality goes through `default: ""/[]`; **every param needs its own
  `description`** (prose in the tool description is not read at argument-construction time). And
  FastMCP validates with **pydantic**, so **any advertised constraint** (`maxItems`, `pattern`,
  `additionalProperties:false`) turns a recoverable input into an `isError` the model must recover
  from = an extra round-trip. Constraints are stated in descriptions and enforced by
  `normalize_keywords`/`normalize_phrase` (`domain/mcp.py`) via `BeforeValidator`, which stays out of
  the emitted schema. Zero results is a **success** (`No records matched.`), never a retry hint. See
  `decisions/mcp_tool_schema_liberal_input.md`. **claude.ai caches `tools/list` at registration —
  a schema change needs the connector removed and re-added.**

**Untrusted-content sandbox** (`docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md`, `decisions/sandbox_render_service.md`) — one
place where bytes the product did not write are processed, holding nothing. **Status: S1 built, not accepted, not deployed**:
only `render.docx` (the model-written Node DOCX script) moves, behind `SANDBOX_RENDER` (`inprocess` default | `sandbox`; no
automatic fallback). The service is a separate Cloud Run unit with a zero-role identity (`alek-sandbox-` prefix, enforced by
`scripts/infrastructure/cloudbuild_sandbox_render.sh`), no secret, concurrency 1, `SANDBOX_RECYCLE=always` (one request per
instance). Invariants I1-I7: holds nothing, one request's data, identifies no one, bounded, not a source of truth, stateless,
engines never share the server's uid. **Two processes in `sandbox_main.py`**: the supervisor (PID 1) and `--serve` (Quart on
Hypercorn, HTTP/2); the engine is a third, a child under the `sandboxengine` uid with rlimits (`src/adapters/sandbox_engine/`).
Callers are verified in-app (OIDC signature, audience, per-op email set); no `SANDBOX_URL` = `unconfigured` (only `/health`,
ops 503; first pass of the two-pass deploy). Code: `src/domain/sandbox_*.py`, `src/web/sandbox_*.py`,
`src/composition/sandbox_*.py`, `src/adapters/sandbox_{engine,client}/`, `Dockerfile.sandbox`. Deploy and the hard stop (remove the
caller's invoker binding, never empty the caller list): `docs/07_deployment/README.md`. Still waiting on the S0 spike (flush barrier,
`RLIMIT_AS` vs Node, NPROC/NOFILE 64/64, work volume size).

**Gmail Email Indexing** — passive inbox-as-memory: OAuth connect (`/auth/connect-gmail`) → paginated
Cloud Tasks → `EmailIndexingService` → `GmailProviderAdapter` → `EmailClassificationAgent` triage →
valuable emails as `IndexedEmail` in `domain_email_facts_v1` (4-vector, mirrors FactEntity).
`EmailEmbeddingRepairService` backfills missing vectors; `email_indexing_watchdog` (every 2h) fails
stale `running` jobs.
- **A revoked Gmail grant is loud, once.** `invalid_grant` on refresh → `OAuthGrantRevokedError`;
  the daily review / indexing worker sets `OAuthCredentials.needs_reconnect` and notifies the user
  only on the False→True transition (a reconnect or successful refresh clears it implicitly).
  `/api/gmail/status` returns the flag and the Cabinet shows **Reconnect Gmail**. Until 2026-10-10
  a revoked grant failed silently while the Cabinet said "Connected". See
  `decisions/gmail_grant_revoked_reconnect.md`.
- **`WorkerHandler`** dispatches `/worker` Cloud Tasks by `task_type`:
  `agent_execution`, `email_indexing`, `email_indexing_watchdog`, `start_email_indexing`,
  `consolidation`, `sweep_consolidation`, `companion_consolidation`, `sweep_companion_consolidation`,
  `deep_research_failed`, `deep_research_webhook_event`, `fire_due_reminders`,
  `execute_reminder`, `setup_microsoft_todo`, `reindex_task_list`, `renew_task_subscriptions`,
  `renew_all_task_subscriptions`, `start_daily_email_review`, `daily_email_review`,
  `billing_daily_summary`, `fact_lifecycle_sweep`, `fact_review`, `interactive_choice`, `repair_email_embeddings`, `start_drive_sync`. Full reference:
  `docs/07_deployment/SCHEDULERS.md`.
  `WorkerHandler` is the only dispatcher: `telegram_update` and a queued Slack event (no `task_type`; `event` +
  `session_id`) go to the adapters attached by the builder (`build_application_graph`);
  any other `task_type` is a 400 (no Slack fallthrough).
  `drive_sync` is not a main-service task at all: the drive queue delivers it to the indexer service's `/worker`
  (`indexer_main.py`).
  **Every Cloud Tasks delivery is claimed by task name** (`TaskClaimStore`, `{prefix}worker_task_dedup`): holder
  process id + attempt id, `running`|`done`, 1 h expiry. Cloud Tasks is at-least-once; a delivery that finds a live
  claim is skipped (200 `duplicate`), and the claim of a dead attempt (another process, or this one but not running)
  is taken over by the retry (one-instance assumption, as in `LongTurnService`). A raise, a >= 500 or a 429 releases
  the claim, so the retry runs. `/worker` logs the `X-CloudTasks-*` headers. The same collection also holds the 48 h
  daily-review marks and the 72 h OpenAI deep-research webhook claims (liveness follows each doc's own `expires_at`).
  See `decisions/worker_task_dedup.md`.
- **Chat intake never loses or doubles a message.** Slack and Telegram tasks have deterministic Cloud Tasks names
  (`ALREADY_EXISTS` = already queued; the queued path uses no intake mark, the inline Telegram path still does). A busy
  session waits in-process up to 100 s (FIFO), then re-queues under a named task (max 12 x 15 s), then posts a
  "resend" notice; non-user Slack events (bot, edits, other types) skip the lock and never get the notice.
- **Deep research reaches the channel that asked.** Origin channel + platform travel on every live delivery path (Claude
  Cloud Run Job, OpenAI webhook, in-service runner). The OpenAI webhook only verifies, filters `response.*` events,
  retrieves the response (events carry only `data.id`), claims the job id and enqueues `deep_research_webhook_event`
  (named by job id); delivery runs in `WorkerHandler`. A failed job enqueues `deep_research_failed` and the user is
  told. The HTML report task has the dedup key `deep_research_report:<job_id>` (Cloud Tasks name `agent_execution-<sha256(key)[:32]>`) and its create is retried under that name;
  `deliver_deep_research` returns whether it was handed off and notifies on failure. The `deep_research_polling` path is gone.
- **Consolidation and companion queues take the oldest pending batch first** (`order_by created_at`, key required).
  Their two composite indexes in `config/firestore.indexes.json` must be `READY` **before** the code is deployed.
- **`UserNotificationService`** — background notifications to the user's last active channel
  (`user_notification_state`). `notify()` routes `system_alert` through a formatter agent (Quick by
  default; `agent_id_override` → Smart for reminders/daily-review) → formatted delivery + session history.
  `notify_raw()`: direct text, no reformatting. `prompt_tokens` in `UsageMetadata` always = uncached input
  (OpenAI/Gemini subtract cached from total).
- **Billing daily summary** — Scheduler 09:00 Europe/Madrid → `billing_daily_summary`: posts yesterday's
  `prev_daily_tokens/cost` snapshot to Slack. Per-provider cache pricing Claude 0.1× (Opus 5.5 / Sonnet 5.5: 0.05×)/OpenAI 0.1×/Gemini 0.1× (3.x generation; was 0.25×).
- **Token accounting is per-execution, NOT per-instance.** `TokenLedger` (`domain/billing.py`) lives in a
  `ContextVar` opened by `BaseAgent._execution_billing_scope()`; `_call_llm` accumulates into it,
  `_flush_billing` reads it. Agent instances are per-user singletons and `DelegationEngine` fans a tool
  batch out via `asyncio.gather`, so instance-level accumulators billed each concurrent execution the
  running total (3.6× inflation, fixed 2026-07-28). The scope's `reset(token)` is load-bearing — an
  inline-awaited specialist would otherwise capture its caller's ledger. **Account counters written before
  2026-07-28 are inflated; for historical cost use BigQuery `prompt_content`, not `usage.*`.**
- **Cost is priced per model that RAN, not per agent default.** `TokenLedger.by_model` keys a
  `ModelUsage` leg per model (`add(model, …)` from `_call_llm` using `request.model_name`, or the
  fallback model after a cross-provider failover); `cost()` sums each leg at its own rates. An
  execution legitimately spans models (Smart's per-request tier, WebSearch's `fetch_url` ECO
  downgrade, failover) — pricing it all with `self.model_name` under-reported Smart and
  over-reported `fetch_url` (fixed 2026-07-30, so cost before that date is wrong too).
  `record_usage(model=…)` receives `dominant_model` as a label only. See
  `decisions/billing_per_model_pricing.md`.
- **Usage legs never overlap.** `calculate_cost` bills `prompt_tokens`, `cache_read_tokens` and
  `cache_creation_tokens` as disjoint legs. OpenAI's `cache_write_tokens` sits *inside*
  `input_tokens`, so the adapter subtracts both cache legs. Until 2026-10-13 it subtracted only
  reads, so written tokens were billed at 1× + 1.25× (OpenAI cost about 24% high since 2026-08-11).
  See `decisions/billing_cache_write_and_voice_seconds.md`.
- **Daily budget alert (advisory, never a gate).** `BillingAccount.daily_cost_limit` (default $5) →
  `increment_account_usage` returns `UsageIncrement`; `FirestoreQuotaService` posts to the ops sink
  (`AlertSinkPort`, `BILLING_SLACK_WEBHOOK_URL`) when the day *crosses* the limit — once per day, not
  while-over; the increment is lock-free except at a daily/monthly rotation (`decisions/billing_usage_increment_lock_free.md`). `check_quota` is dead **by decision** (alert-only; owner dropped the hard cap 2026-07-26).
  See `decisions/billing_execution_scoped_ledger.md`.
- **Daily Email Review** (`gmail_daily_review*` in `UserBotConfig`; hourly `start_daily_email_review`
  fan-out → per-user `daily_email_review`; once per local date, at the first hourly tick within 3 h of the configured
  hour, wrapping midnight; mark `daily_review:{user}:{date}`, named task; invalid tz → UTC) — fetches last-24h emails (cap 200, full body, cleaned by
  adapter: BS4 + `html.unescape` + invisible-Unicode strip), passes a structured JSON array to Smart via
  `notify(save_history=False)`. Smart protocol: triage ([ACTION]/[FYI]/[DIGEST]/[NOISE]) → deep-reads
  (`get_email_details`/`get_email_attachment`) → `search_web` → `create_html_page` report (Gmail-linked
  subjects) + short chat message. `notify_document_link` saves report URL + `fetch_url` hint to history.
  Cabinet: `/api/gmail/daily-review`.

**Voice Companion (Lelik)** — a phone call to the exocortex, not a chat surface. The owner dials the
Twilio number; `/voice/auth` resolves the caller's `From` to a user (platform `"phone"`), enforces a
one-call-per-user marker, mints a short-TTL **ticket** and parks the callback under the inbound
`CallSid`. Only when that dial ends (`/voice/inbound-status`, `completed`) does `LelikAgent` originate
the *callback*. Placed any earlier, it hit the inbound dial's teardown and the carrier returned SIP
480 (inbound dial is never the conversation leg — the callback is what proves identity).
Machine detection is **async** (RFC §4.6): the callback connects at once, the `AnsweredBy` verdict
lands on `/voice/status`, and it only gates memory — a `machine_*` call with under three caller turns
gets no summary (`domain.voice_amd.is_voicemail`). Sync AMD hung up on an owner who opened with a
request (>2.4 s of speech reads as a greeting). On pickup, `/voice/answer` has `LelikPersonaService` assemble Lelik's prompt and returns TwiML
pointing Twilio's Media Stream at the relay. **Lelik starts warm, not as a front desk** (owner
decision 2026-09-22, `decisions/lelik_warm_context.md`). He gets only the facts of the domains in
`UserBotConfig.voice_fact_domains` (allowlist, default `biographical` + `preference`; the rest is
reachable through `search_memory`), **no standing directives** (they are written for the text
orchestrator; revised 2026-10-21), and the primary channel's last 30 messages as stored summaries
(no LLM call; Smart writes them in Alek's own character with emojis, up to 60 words / 600 chars), read through `UserNotificationService.resolve_channel`. That is the same chain the call
summary is written through, so past calls reach the next one. He talks from that and forwards to
Alek only what he does not hold (mail, tasks, documents, web, actions). **Character is shared
with Smart:** the `lelik` profile reuses Smart's overridable `ARCHETYPE_*`/`VIBE_*`/`VOICE_*`/
`HUMOR_*`/`LANG_*`/`POLICY_*` slots, and user prompt overrides are per user by class+category, so a
persona or language change applies to both. Lelik has only two tokens of his own:
`COGNITIVE_PROCESS_LELIK` (role) and `SPOKEN_DELIVERY` (call mechanics only — unreadable medium,
confirm before irreversible actions, silence/delegation hosting — in its own category so a
`VOICE_*` override never replaces it) (RFC §4.1/§4.8). **How he sounds lives in one place:** the
spoken paragraph of `build_persona_anchor` (a named register — spontaneous phone call — not
per-sentence prosody rules, which read as a narrator; plus "configured language is the default,
not a lock"). The caller opening is only a greeting cue. Three sources of delivery rules
contradicted each other until 2026-09-28; see `decisions/lelik_delivery_single_source.md`.
- **Two deploy units, on purpose.** The main Quart service (`main.py`) owns Twilio's webhooks,
  ticket minting, and the OIDC-protected control plane (`/voice/session-config`,
  `/voice/submit-transcript`). A **separate Cloud Run service** (`relay_main.py`, a plain
  `websockets` server — not Quart) owns the live audio. The split is forced by Cloud Run, not taste:
  an open WebSocket is one long request, so a 20-minute call bills 20 minutes of CPU and would
  compete with Slack/Telegram serving on the same 1 vCPU box, and the relay needs `--timeout=3600`
  which must not be applied service-wide (RFC §4.14; a call over 60 min is cut — accepted v1 limit).
  Same precedent as DeepResearch's Cloud Run **Job**. Session affinity is explicitly not needed — the
  WebSocket *is* the session, and the relay always initiates toward the main service.
- **The ticket is the only identity handoff between them.** It rides in the webhook **query string**
  (Twilio leaves it untouched and its HMAC covers the full URL, so it cannot be swapped) and is
  **atomically consumed** by `/voice/session-config` via `get_and_delete` — single use, so a captured
  ticket cannot be replayed to read back the owner's persona and facts.
- **Audio is μ-law 8 kHz end to end** — Twilio and OpenAI Realtime both speak it, so the relay is a
  byte forward with no decode/resample. Turn detection is `semantic_vad` (eagerness low) with the
  provider's `interrupt_response` **off**, so `VoiceSessionService` alone owns barge-in. It reads
  the heard milliseconds from `PlaybackTracker` (Twilio `mark` echoes) *before* clearing, then runs
  `clear` → `response.cancel` → `conversation.item.truncate`, so the model knows where it was cut
  off. Only speech lasting 1 s interrupts (`barge_in_min_speech_s`); shorter blips are dismissed and
  their turn gets no reply. Speech that starts in the greeting's first 3 s never interrupts it
  (`greeting_guard_s`): the browser's echo canceller is still adapting, and Lelik's own greeting came
  back as the caller and made him greet twice. A relay-side watchdog handles silence (the provider's `idle_timeout_ms` is server_vad-only):
  8 s of quiet after playback ends injects one system note (and 20 s more of silence after it hangs up) — except while a delegation is pending,
  when it re-arms on its own after a random 8–15 s (`waiting_gap_max_s`) and has Lelik fill the pause instead, standing down while an answer is
  mid-injection so the answer always wins the reply slot; a tool call from a response the caller
  already barged into is answered "not run" rather than dispatched. A breath-pulse "thinking cue"
  for silent gaps is built but OFF (`relay_main.py` does not pass the clip): with it on, replies
  were cut after ~0.4-0.8 s on live calls, cause not yet found. `create_response` is off, so
  every reply is started by the relay after appending the text path's `build_persona_anchor` as a
  `system` item. Anchors are never deleted, because a failed delete is a call-ending provider error
  (RFC §5.2). Lelik's own lookups post nothing to chat (auto link copy withdrawn 2026-09-29).
- **Two search depths, questions vs errands** (VOICE_COMPANION_RFC §4.15, 2026-09-29).
  - Lelik's own lookup is `search_web_light`: one grounded Gemini ECO call, ~2 s. The full
    `search_web` with Maps fan-out (11–15 s) left his allowlist; deeper research goes to Alek.
  - `ask_alek` is a question: SYNC, and Lelik hosts the wait. `tell_alek` is an errand: ASYNC, so
    it runs as a **Cloud Task**, not a background task in the route, because Cloud Run throttles the
    CPU once a response is sent.
    - Alek posts the errand's whole outcome to chat.
    - `AgentWorkerHandler` reports a failed errand (`_notify_errand_failure`).
  - Smart's `PROTOCOL_VOICE_PARTNER` (`lelik_request_quality`) tells Alek that Lelik's request comes
    from a weaker model: infer the user's intent from their own lines (12 call turns now) and the
    chat history, and serve that.
- **Two agents, deliberately not one.** `LelikAgent` is a standard delegating agent (Slice 2), not a
  call-placer only: three entry points — `execute()` places the callback, `session_config()` builds
  the realtime session's instructions + tool declaration (over `LELIK.allowed_intents`, the same
  `_build_delegate_tool_declaration` Smart/Tutor use), `delegate()` runs one `delegate_to_specialist`
  call from the live session through `DelegationEngine.dispatch` (made public for this second caller).
  `LelikSummarizerAgent` (ECO) turns the end-of-call transcript into a short note written as Lelik
  himself (first person, his `VOICE_*_LELIK` tokens, humour and emojis, up to 60 words; revised
  2026-10-21) in the user's `LANG_*`, and one line when nothing is worth keeping. It is delivered via
  `UserNotificationService.notify_call_summary` as the history pair `[System: phone call with Lelik, …]`
  + `📞 <note>`. Smart's `PROTOCOL_VOICE_PARTNER` tells Alek who Lelik is and how to read that pair. The summarizer is a separate cheap model
  working from the transcript, never the live session it summarizes (RFC §4.9) — it speaks in
  Lelik's voice, it is not Lelik's session — and it has no `CompanionRecord` store — `records` is always `[]`.
- **`POST /voice/delegate`** — the relay forwards one `delegate_to_specialist` call raised inside the
  live session to this OIDC-protected route (`voice_control_plane_app.py`), which resolves the
  caller's `LelikAgent` via `get_lelik` and runs `agent.delegate()`, returning the specialist's text
  for the relay to speak back.
- **`resolve_late_answer`** (`VoiceSessionService`) is the one seam for the delegation-answer policy
  (RFC §4.7): a `response.create` while a response is already active is a call-ending provider error,
  so it queues the answer when the model is mid-turn and injects + replies otherwise.
- **The relay's own delegation timeout is 300 s** (`asyncio.wait_for` in
  `VoiceSessionService._fetch_answer`, raised from 90 s after UAT round 1, 2026-09-28) — the HTTP
  client (330 s) and the `ask_alek` path's own 600 s ceiling (`ASK_ALEK_TIMEOUT_MS` on the
  gateway's `AgentConfig` + the routed message's `timeout_ms`) both outlast it. A delegation that
  finishes after the relay stops waiting (timeout, or call end) is posted to the caller's chat
  instead of being discarded — both sides write a marker and whichever claims the finished result
  with an atomic `get_and_delete` posts it exactly once; see RFC §12.
- **No dispatch filler** (removed 2026-09-29, RFC §4.15). A `_DISPATCH_NOTE` used to fire right after
  a delegating turn, and Lelik voiced a second "it's on its way" after his own "checking". The
  dispatching turn's acknowledgement is enough. A long wait still gets the watchdog's `_WAITING_NOTE`.
- **Lelik's register** (copilot, RFC §4.15):
  - his profile has its own `VOICE_COPILOT`, `HUMOR_COPILOT` and `FEW_SHOT_EXAMPLES_LELIK`
    (situation + good + bad reply), all system tokens, instead of Smart's aphoristic voice,
    Ranevskaya humor and few-shots;
  - archetype and vibe stay shared with Smart;
  - the model voices any rule about the *form* of its reply ("briefly", "no ceremony", "I won't
    make it up"). Steer with examples, not with rules about form.
- **No caller picks sync/async** — every intent runs in its manifest-declared mode. The per-call
  `mode: "now" | "later"` on `delegate_to_specialist` was removed 2026-10-06 (Lelik used to strip
  it: "later" on a SYNC intent dropped the answer). See `decisions/delegate_mode_parameter_removed.md`.
- Per-user config (prompt builder, persona service, …) is cached with the agent, same ≈1 h TTL as
  Smart and Tutor. `get_lelik` calls `ensure_agents_for_user` on every use, refreshing that TTL — which
  also keeps the user's Router warm for `ask_alek`, the same mechanism every other entry point relies on.
- `/voice/delegate` flushes `PromptContentStore` writes before responding, like `/voice/submit-transcript`
  — Cloud Run throttles CPU once the response is sent, so background BigQuery writes left pending fail.
- Usage is priced by `domain.billing.calculate_realtime_cost` over the flat legs
  `OpenAIRealtimeAdapter._flatten_usage` produces; `reasoning_tokens` are a **subset** of the output
  text leg, never additive. RFC: `docs/10_rfcs/VOICE_COMPANION_RFC.md`. Deployment prerequisites
  (relay service, `VOICE_RELAY_STREAM_URL`'s two-pass first deploy, Twilio secrets):
  `docs/07_deployment/README.md`.
- **Realtime provider is per user** (`docs/10_rfcs/VOICE_MULTI_PROVIDER_RFC.md`).
  - `UserBotConfig.voice_provider` picks an entry of `domain.voice_provider_profile.VOICE_PROVIDER_PROFILES`:
    the prompt profile, turn ownership, voice and effort. When unset, `DEFAULT_VOICE_PROVIDER = "xai"`
    applies (xAI `grok-voice-3.0`, prompt `lelik_xai`, voice castor).
  - `LelikAgent.session_config` puts the spec in the ticket under `"voice"`. The relay builds the
    adapter per call from it (`relay_main._ADAPTERS`). A ticket without it runs the legacy OpenAI
    session.
  - **Turn ownership is per call.** `RELAY` (OpenAI): anchor, relay-started replies, the relay's
    barge-in and silence notes. `PROVIDER` (xAI): the provider replies and stops itself, and the
    relay only drops queued audio and delivers the greeting and Alek's answers.
  - **Each adapter declares its turn ownership** (`supported_turn_ownership`). xAI ignores
    `create_response: false`, so it is PROVIDER-only.
  - **Lelik's prompt differs per provider.** `lelik_xai` is a second-person portrait in xAI's
    prompting-guide format, with none of the gpt-realtime rules. See
    `decisions/voice_xai_protocol_probe.md`.
  - **The voice character is USER-overridable per category** (RFC §4.5): `voice_persona`,
    `voice_humor` and `voice_temperament` can be overridden; `voice_care` and
    `voice_call_manners` cannot. These are separate from Smart's `archetype`/`vibe`/`humor_engine`,
    so a text-persona override never reaches the voice.
  - xAI audio is billed per second. `billable_audio_seconds` on `response.done` is the session's
    **running total**, so `XaiRealtimeAdapter` emits the increment. Summing the raw value billed a
    4-minute call as 31 minutes until 2026-10-13
    (`decisions/billing_cache_write_and_voice_seconds.md`).

**Voice web transport** (`docs/10_rfcs/VOICE_WEB_TRANSPORT_RFC.md`) — a second call kind, a
Cabinet call page over WebRTC, next to the Twilio phone path, sharing the same `VoiceSessionService`
and Lelik persona. Cloudflare's Realtime SFU dials the same relay's `/sfu/*` routes (not a new
deploy unit); `SfuStreamHandler` pairs the ingest/egress WebSocket legs by ticket and drives
`PacedAudioOutlet` for provider→browser pacing at wideband PCM (24 kHz to the model, 48 kHz stereo
to the SFU), vs. Twilio's narrowband μ-law. Main-service side: `MediaRoomPort` /
`CloudflareSfuAdapter` mint SFU sessions over HTTPS. `VoiceCallSetupService` (extracted from the
Twilio webhook's inline setup) does persona + ticket prep (`prepare`, keyed by `call_kind`
`phone`/`web` so the pickup note and summary header read right) for both entry points; the web path
also uses it for the one-call marker, while Twilio's `/voice/auth`, `/voice/inbound-status` and AMD
paths still manage marker and ticket inline. The web marker holds a short setup TTL and is extended
to the call TTL only when the relay redeems the ticket in `/voice/session-config`. Because both SFU legs must land on the same process, the relay's
`--max-instances=1` (already required for other reasons) is now also a **pairing constraint**, not
just a cost choice — scaling calls up means relay shards (N single-instance relays, main service
picks one per call), not raising this number. Two relay-only knobs, `os.getenv` not
`load_settings()` (optional, not secrets): `VOICE_WEB_REASONING_EFFORT` and
`VOICE_WEB_CALLER_OPENING`, both defaulting to match the phone path (`reasoning_effort` is `medium`
on both voice paths as of 2026-09-27, no longer `high`).
- **On a phone, `/cabinet/call` is the Cabinet's front door** (RFC §14): a bare `/cabinet` on a
  touch device redirects there. The page is Home-Screen installable
  (`/cabinet/call/manifest.webmanifest` + icon routes; the app has **no static folder**, so
  `/static/*` 404s). Siri = a Shortcuts "Open URL" action. **The Cabinet session is capped at 24h
  with no silent refresh** (owner rule: sensitive data), and refresh tokens older than the current
  TTL are rejected. See `decisions/cabinet_session_24h_cap.md`.

**Consolidation** — Owner's map: `docs/05_building_blocks/memory/README.md`.
Long-term memory formation: sliding window fills → batch to Cloud Tasks queue →
ConsolidationAgent ("Life Chronicler") extracts facts/principles from raw messages (non-blocking).
Thresholds are global: threshold=70/batch=50 live (`domain/settings.py`; the `is_production` branch in `config/settings.py` sets 50/30);
`UserBotConfig.consolidation_threshold` has no effect anywhere, `consolidation_batch_size` only sizes the `$consolidate`
cut. Consolidation writes skip the number-aware 0.96 dedup (`skip_deduplication=True`): the model decides duplicates
after searching; the 0.96 check runs only on the non-live v2 path. 3 vectors/fact + versions: a content change of `update_fact` writes a new document (same `lineage_id`, `version`+1, the old one closed with `superseded_by`) in one preconditioned transaction, a lifecycle change stays in place; a stale id is followed forward (`decisions/fact_versions_one_writer.md`). One writer per account: every consolidation batch runs under a Firestore memory-write lease, a loser defers itself with a named task (`decisions/fact_versions_one_writer.md`, memory map §3); biographical cache updated for next conversation.
**Serialization:** model parts use `p.text` (summary), NOT `p.full_text` (verbose + web_search_context);
user parts `p.consolidation_text or p.text` (`consolidation_text` is legacy, see below).
**Loop alerts:** a pass that hits the turn cap without a final report posts a 🚨 ops alert (`AlertSinkPort`);
while `CONSOLIDATION_STATS_ALERTS` (`os.getenv`, default on, temporary since 2026-10-20) is on, every pass
posts a 📊 stats line. See `decisions/sonnet55_adaptive_thinking_stall.md`.
**Expiry and the janitor (Facts S3, deployed 2026-10-27):** the consolidator's writes carry an `expiry` (`date` / `horizon` /
`none`) that code turns into `expires_at` in the owner's timezone (map §2.7); a daily zero-LLM janitor (`fact_lifecycle_sweep`)
stales due facts behind a breaker and **writes since the deploy of 2026-10-27** (owner's word; rollback: remove `FACT_LIFECYCLE_SWEEP_DRY_RUN` from the deploy, which makes it log-only again); never a TTL policy on the facts collection (a test scans the
tree). See `decisions/fact_expiry_and_janitor.md`, map §5.5.

**Memory review (Facts S4, deployed 2026-10-27)** — what a stale fact becomes. The janitor (above) enqueues a `fact_review`
task per account (daily by default, `fact_review_interval_days`, log-only mode too). `FactReviewService` runs it under the
memory-write lease: housekeeping, timeouts in code, selection of up to 20 stale facts, one pass of the consolidation agent
with its own profile (`consolidation_review`; tools `update_fact`, `ask_owner`, `count_words` only; `FactReviewScope` refuses
a revive, rewrite or invalidation without evidence or the owner's reply, an archive without the owner's reply, and
`date_passed` except on an `as_history` rewrite), then the send of the next question and the weekly digest. **P2 revised
(owner 2026-10-27; deployed 2026-10-27, the janitor writes):** a fact whose date passed is rewritten as
**history** (a version at `archival` priority, `expiry none`: happened / planned and did not happen / planned, unknown), never
archived; archive is kept for noise, an end the owner states and the Cabinet (RFC §9 "P2 revised"). What the pass cannot
decide becomes a **question** of a kind (`still_true`, `happened`, `renewed`, `paid`; options per kind, the answer texts
written by the pass at ask time), one fact per message, written by code (never a model), one at a time, at most 5 a day, to
the primary chat only. A routine invoice with no payment confirmation is not asked in chat: it becomes "payment not
confirmed" history and a **digest question**, listed in the weekly digest and answered in bulk in the Cabinet; a fine or a
debt is asked `paid`. The fact is **current while its question waits**; the janitor skips it and
counts it in the *pool* (stale + waiting facts, capped at 50). An answer is only a reply to the question message, a Slack
button (`POST /slack/interactivity`: signature checked on the raw bytes first, the work done by the `interactive_choice` task)
or a Cabinet click, applied by `FactAnswerService` (no model, no lease, same preconditions); words are read by the review once.
Silence for 14 days applies the question's fallback: revive (twice the last window, 14 to 365 days), archive (noise only),
or history (the question's "unknown" text written through the typed path under the `timeout` actor and listed in the
digest; a failed write is retried on the next runs, at most 3 attempts, then the question closes `resolved_elsewhere`).
Closed questions and digest claims carry `purge_after` for a Firestore TTL policy; never `expires_at`. The 28 message texts are UI
strings (uk, en, fr, es) or model-written under code checks; every text the pass writes is translated like the quotes. Text the bot did not write (a stored fact, a translated
acknowledgement) is sent literally (`send_literal`, `notify_raw(literal=True)`). **Cloud Tasks names dedupe only while a task is
queued or running and for about an hour after**: a name like `fact-review:<account>:<local date>` merges runs that start close
together; "once per local day" for close runs only. The daily rhythm comes from `review_due`, not from the name. See
`decisions/fact_review_and_owner_questions.md`, map §5.6.

**Memory facts the owner confirms** — the bot never writes to memory itself. The consolidator reads
every user message as the owner's own words, so the removed `save_to_memory` intent let bot-written
text (procedures, file notes) become facts and `AGENT_DIRECTIVE`s. Now Smart's local tool
`propose_memory_facts` (interactive turns only, max 20 facts) stores a 7-day draft tagged with
`origin_session_id`; the owner reads the facts in a `memory-facts.txt` file (chat adapters truncate
long text) and pastes `$memory save <code>`. `MemoryFactService.confirm` appends a user/model pair to the
**origin** session with `append_messages_batch(..., raise_on_error=True)` (the session store otherwise
swallows errors) and deletes the draft only after that. The facts must first appear in that user message,
never in a model message (the consolidator's echo-trap rule discards what an assistant stated). Residual:
Smart sees only the last 30 messages, so the pair can leave its view before consolidation. Legacy:
`MessagePart.consolidation_text` has no writer; its readers stay until stored sessions drop the field.
See `docs/10_rfcs/MEMORY_FACT_CONFIRMATION_RFC.md` + `decisions/memory_facts_owner_confirmed.md`.

**Standing Directives** — user behavioral rules ("never give partial answers", "trace conditional
logic before judging") are a first-class fact domain `FactDomain.AGENT_DIRECTIVE`, rendered as a
binding `standing_directives {}` block in the system prompt of every agent built with the biographical context
(Smart, Quick, Notes, Tasks, EmailSearch, PdfGenerator; the consolidator and Lelik excluded) on every request (NOT
biographical `preference`, NOT firing self-reminders). Rendering = a channel split at
`PromptBuilder.build_for_agent` (mirrors `query_specific_context`): partitions the flat cache list
into `static_bio` / `directives`, each its own `assemble()` param; gated by `include_directives`
(False for the consolidator — it curates them as records, never obeys). Curation = **unconditional
Stage 2b** (`ConsolidationAgent._review_directives`, runs every consolidation, fed the FULL rulebook)
that optimises them as a system-instruction section: imperative English (except quoted literals the
agent must output/match), terse, one rule each, no overlap — *convergence not churn* (already-optimal
→ NO-OP). **Hard cap 15, two layers:** prompt (merge adjacent, else invalidate least essential — no
"umbrella" merges of unrelated behaviors) + deterministic code backstop `_enforce_directive_cap`
(invalidates lowest-priority tail if LLM left >15); injection independently bounded by
`DEFAULT_DIRECTIVES_CACHE_LIMIT=15`. See `decisions/standing_directives.md` +
`docs/10_rfcs/STANDING_DIRECTIVES_RFC.md`.
- **What qualifies (2026-08-18): SUBJECT × SCOPE, decided at creation.** `Directive_Maintenance`
  in the `CONSOLIDATION_TAXONOMY` token requires BOTH — the rule is about how the agent behaves, AND
  it is in force across most requests. Passes SUBJECT but fails SCOPE → `PREFERENCE`, rewritten to
  state its condition (retrieved by relevance instead of always injected). Routing by subject alone
  is what filled the rulebook with situational rules; one of them ("never judge from surface-level
  text parsing") misfired on a map question and burned a whole interactive budget.
- **Stage 2b also REMOVES, not only rewrites.** Its removal branch used to sit *inside* the cap
  section, so below 15 it never fired — baseline emitted 0 ops on a 14-record rulebook. It now has a
  standing duty (DEMOTE / INVALIDATE / MERGE) on every pass, the convergence guard scoped to
  *wording* only, the SCOPE test **by reference** to `Directive_Maintenance` (one definition, not
  two), and a checklist forcing one `scope_ok`/`reminder_dup`/`unusable` line per record before any
  operation. Demotion is necessarily two calls — `update_fact` never touches `domain`.
- **Expect dispersion, not determinism.** Identical rulebook, prompts and model produced 0 and 6
  demotions on two consecutive production runs. Do not read one pass as evidence that the rule
  changed. See `decisions/directive_applicability_gate.md` § Variance.

**Agent Skills** — named procedures for Smart, loaded on demand (`docs/10_rfcs/AGENT_SKILLS_RFC.md`,
delivery A + B both live). A user's custom skills live in Firestore (`{prefix}skills/{user_id}:{name}`,
`versions/v<n>`); system skills ship in git (`src/skills/smart/<name>/SKILL.md`, currently
`skill-creator` + `domain-competency-research`) and are loaded once at startup by
`load_system_skill_bundle` (`src/adapters/filesystem_skill_loader.py`, with their text files) — a malformed one fails startup.
`SkillService` merges system ∪ the user's custom skills; Smart fetches the merged catalog once per
request (`SkillService.list_skills`), renders `available_skills {}` before `standing_directives`,
and serves `use_skill` through `DelegationEngine(local_tools=)` — not via `delegate_to_specialist`.
A loaded body is persisted as a raw `[Skill "<name>" v<n>]` block in `full_text` and a neutral stub
in the summary, so it lives until history tiering; after that the model can reload it with
`use_skill`.
**Authoring (delivery B):** on interactive turns only (`ConversationHandler` sets
`agent_context["interactive_delivery"]`; background paths — `notify`, `ask_alek`, `tell_alek`,
`/worker` — never set it), Smart also gets `draft_skill`: it stores an immutable draft and returns
a `skill_preview` delivery, which `ConversationHandler` alone delivers in order — the verbatim
`SKILL.md` as a file, each file the model wrote as its own file (at most 5, delivery C), a change
summary when the skill has files, then a separate message with only `$skill save <code>` (not
posted if any earlier post fails). Pasting that code is
the save authorization; nothing is active before it. Commands, handled in `ConversationHandler`
before any LLM call (own skills only): `$skill save <code>`, `$skill list`, `$skill delete <name>`
(bare `$skill` prints usage). Both adapters strip a whole-text backtick wrapper before the `$` check
so a copied command still dispatches; Telegram never treats a forwarded message as a command.
Drafts live in `EnvironmentConfig.skill_drafts_collection`.
**Files (delivery C, RFC §15):** a skill carries text files as `files/{sha256}` docs under its index
doc, read as `skill:<name>/<path>` refs through `open_file`; a draft lists only file changes
(unlisted files carry over), and the preview posts each model-written file as its own file plus a
change summary between `SKILL.md` and the save command.
Personal skills are never system skills — the repo is public.

**Agent Workspace** (`docs/10_rfcs/AGENT_WORKSPACE_RFC.md`, owner decisions D1–D17) — Smart's own files for its
work, a knowledge base it builds and follows, in a dedicated versioned GCS bucket (`GCS_WORKSPACE_BUCKET`: declared in
`load_settings()`, a Secret Manager secret in the main service's `--set-secrets`; local dev shares the live bucket, D9).
Key `{user_id}/<folder>/<path>`; folders `projects/` (one file per task or recurring job), `references/`, `scratch/`
(deleted 7 days after the last write via `customTime`), `files/` (byte-exact copies of chat uploads and delivered
documents). A file has no required structure: how to use it is the instruction's business (D16). **Pull-only (D1):** no
index in the prompt; Smart touches it only when the user asks, an instruction it follows (a skill, a reminder) names a
file or asks it to keep state, or a firing reminder carries a workspace file. One local tool `workspace` (`view create
str_replace delete rename copy history restore`, `src/infrastructure/workspace_tools.py`), declared on every Smart path
where the service exists, outside `if skills:`. The handler is built per execution: one `asyncio.Lock` taken first by
every command (a batch applies in emission order), mutations under `asyncio.shield` (the shielded call records the
generation and logs even when the run is cut), a `{path: generation}` session map (`create` over an existing file needs
a `view` in this execution; `str_replace` self-guards and re-applies once on a conflict). `WorkspaceService` holds the
rules — path rule, kinds, caps (100 files / 100 MB / 10 MB / 256 KB text; `rename` not checked), `files/` copy-only
(text may be copied into any folder, binary only into `files/`), a neutral view header (path, generation, size, last
writer), the same-fire line (a re-run of a reminder fire is told the version from before the fire), delete/rename
warnings, one `[Workspace]` log line per command. `GcsWorkspaceAdapter` puts under generation preconditions and deletes
as a metadata patch + delete of the **live** object through a fresh blob under `if_generation_match` — never a
generation-addressed delete, which erases the version — and reads back `write_id` / `delete_id` on a 412. Every object
carries its `origin` (`chat`, `background:<task_type>`, `voice:ask_alek|tell_alek`) and, on reminder runs, the fire key
`reminder:<note_id>@<due_at>` — context keys `run_origin` / `run_fire_key` set by `notify(origin_detail=, fire_key=)`,
the Alek gateway, or derived from `interactive_delivery`; origin and history serve debugging and undo, not protection
(D17: no "data only" framing, injection is the fingerprint's class-B work). Reminders gain one optional code-bound field
`workspace_file` (any valid path; bound by NotesAgent's code so its LLM cannot lose the path, D15), rendered in the
alert as one line `Workspace file: workspace:<path>`, in `active_reminders`, the Router's `agent_notes`, NotesAgent's
list and receipts, and the Cabinet (read-only). `workspace:` refs are read by `FileConversionService` through the port;
`open_file` accepts only `workspace:files/…`. Nothing from the workspace enters session history. Review:
`scripts/validation/workspace_review.py` (owner-run, by day 28; includes per-file growth). Decision:
`decisions/agent_workspace_storage.md`.

**Prompt Builder (Token System)** — assembly, not hardcoded prompts: verified Tokens (humor, voice,
cognitive process…) + static Blueprints with `{{CLASS_NAME}}` slots; 4 priority levels
USER > ACCOUNT > AGENT > SYSTEM; static template cached in-memory (24h TTL, 5ms vs 110ms cold). Runtime
context appended as `knowledge_base {}`. `PROMPT_CACHE_BOUNDARY` splits the final prompt: static prefix
cached by Anthropic (5 min), dynamic suffix (agent_notes + Q-S context) fresh every request.
- **Datetime injection disabled by default** (`include_datetime=False`) — time injected via
  `_inject_timestamps()` in user messages (user tz from `UserBotConfig.timezone`) + UTC prefix on
  delegation queries. Opt in with `include_datetime=True`.
- **User location** → `knowledge_base { user_location }` when set (`UserBotConfig.location`, Cabinet
  `/api/user/location`).
- **Injecting large static content** — background tasks pass a large static dataset (email triage
  payload, corpus) via `extra_static_blocks: List[str]` on `build_for_agent()`; placed in the **static
  section** (before `PROMPT_CACHE_BOUNDARY`, after `knowledge_base {}`) as a named Groovy block
  `name {\n…\n}`, referenced by name in the user instruction. Never embed large payloads in the user
  message (pollutes history + bypasses caching).

**File Storage Pipeline** — upload: ConversationHandler → `FileConversionService.process_attachment()` →
GCS → reference-only MessagePart (no content in history). Fetch: specialist delegation with `file_ref` →
`AgentCoordinator._resolve_file_refs()` downloads + converts + injects `file_content`; FileManagementAgent
handles direct `open_file`/`delete_file`. `GcsFileStorageAdapter`: Finder-style dedup + sanitization;
conditional on `GCS_MEDIA_BUCKET`. **Bound channels:** ConversationHandler strips `path` from `file_data`
(`mode.is_bound`) — agent sees `[File: name (size)]` label, accesses via `open_file`.
**A file that never arrives** (`download_file` → None) leaves a `[System: …]` note in its slot via
`file_conversion.download_alert()` — same contract as the size/conversion alerts, NOT a Quick fallback
(the primary agent did not fail; one input did). Temp-file names are bounded by `safe_temp_suffix()`
in **bytes** — NAME_MAX is 255 bytes and a Cyrillic character costs 2. See
`docs/05_building_blocks/file_storage/README.md` §2.1.1.
- **Voice messages never enter this pipeline.** A Slack voice memo / Telegram voice note
  (`FileAttachment.is_voice_message`) is transcribed in ConversationHandler *before* the file path and
  the transcript becomes `context.text` — the user's own turn. Through the pipeline it would be a
  reference-only part, absent from history, so consolidation would get the synthetic "look at this
  file" fallback instead. Model `gpt-transcribe`; spoken languages are a per-user setting
  (`UserBotConfig.voice_languages`, ISO-639-1 list, NOT `LanguageCode` — that enum has no `ru`).
  Slack's marker is the file name `audio_message.*`, never the mimetype (seen as both audio/mp4 and
  video/mp4). See `decisions/voice_message_transcription.md`.

**User Drive** (`docs/10_rfcs/USER_DRIVE_RFC.md`) — the user's long-term file area: the OneDrive App Folder
(scope `Files.ReadWrite.AppFolder`; its localised root path is read from Graph) behind the provider-neutral
`UserDrivePort` (`OneDriveAdapter`; Graph modules in `src/adapters/microsoft/`: `graph_auth.py`, `todo_adapter.py`,
`onedrive_adapter.py`). Token refresh is shared with To Do: concurrent forced refreshes are deduped and the cache
lives at most 5 minutes per provider key, so a disconnect reaches every instance within 5 minutes. The model and
history see only `drive:<opaque id>` refs and `[Drive: …]` labels with decoded paths — no provider name. Graph
paths came back raw in the spike; the adapter unquotes (no-op on raw) and strips both `/drive/root:` and
`/drives/<id>/root:` prefixes. **Smart reaches the drive through one intent, `work_with_drive_files`
(`docs/10_rfcs/DRIVE_SPECIALIST_RFC.md`, `decisions/drive_specialist.md`):** a natural-language commission plus one
field, `file_contents` (texts to save or append, bound by code, never retyped). The `DriveAgent` specialist does the
work in its own loop (≤ 6 turns, 150 s soft deadline under the caller's budget) through local CRUD tools
(`search_drive`, `search_deleted_recordings_and_notes`, `list_folder`, `open_drive_file`, `get_recording_link`,
`copy_file_to_drive`, `save_text_file`, `create_folder`, `move_file`, `append_to_file`, `replace_file_content`,
`delete_drive_item`, `find_passages`, `read_passages`, `report_to_orchestrator`), each calling one method of the user's `DriveOperations`
(`src/infrastructure/drive_operations.py`, FileManagement's former drive code, one instance per user shared with
FileManagement: lock, shield, receipts and late-failure notices such as `🗑️ Deleted from the drive: {path}` and
`⚠️ Deleting {path} did not complete.` (`src/locales/*.py`), both sent literally through `send_literal`, so a path is never
formatted, linked or read as a mention). Code builds the answer: `Drive result: <status>`, a
"What to do" line, op lines, reading lines, then every read block verbatim in run order (vision files as a list); the
specialist's model never sees an opened body (a plain-text final reply beside `report_to_orchestrator` fills an empty
report note). **Answer from content** (RFC §28, built, not deployed): a content question is served by `find_passages` /
`read_passages` (the stored chunks nearest the question, verbatim, one consolidated block to Smart; the specialist sees them
in full); on a run that is not a chat turn (reminder, `notify`, call, no origin) a change is refused once passages were read
(`decisions/drive_content_write_gate.md`). The 8 drive intents it replaced are gone (a stale text naming one gets
the coordinator's unknown-intent refusal). **Searches per commission:** no cap in production (the owner dropped a
3-search cap): ≤ 6 turns, one re-search after a "Without X" line, an identical search skipped by code. A subject is
searched once, in the user's words; synonyms and translations are not searched (a list of alternatives is not a list of
searches, RFC rev 4.5 #7, `execute_as_given` in `COGNITIVE_PROCESS_DRIVE`). G3 fails a case run with more than 3 search
calls (`SEARCH_CALL_CEILING`): an eval check, not a production cap. Store rules: mutations refuse a ref from the other store, reads are lenient;
copying refuses a `drive:` source; a composed text is md/txt/csv/json/yaml/html/svg/xml, UTF-8, ≤ 5 MiB (RFC §4.7.1).
`prefetch_file_ref=False` on both FileManagement and the specialist (a `DriveNotConnectedError` during another
specialist's pre-fetch logs WARNING).
`UserDriveService`: case-insensitive folders, saves never overwrite (same bytes → existing, else suffixed name,
NFC-normalized comparison), append-only text edits, root protected, a move into the folder's own subtree and a rename
onto a taken name ("A file with that name already exists there.") refused. Mutations of one user run one at a time under
`asyncio.shield`; a mutation waits at most 100 s (clamped to the specialist's deadline), otherwise answers success-shaped "still running"
(check with a listing) — never a false "failed", but a provider `TimeoutError` inside a mutation is a failure;
delete/replace post a localised `notify_raw` receipt, or a "did not complete" notice if they fail after "still running" was answered;
a still-running move, copy, save or append gets no later message, and a later change in the same commission is
`not started: an earlier change in this request is still running`. Vision only for JPEG/PNG/GIF/WebP and PDF (HEIC refused). Refs
survive turns via `history_context["drive_context"]` (full_text only, never consolidated). Provider search does not
work inside the App Folder (spike A6), so finding files goes through our own index (below). The `Trivial_Exclusions`
line that keeps file operations out of memory was applied to the live `CONSOLIDATION_TAXONOMY` on 2026-10-10 (dry
run: leaks 3/3 → 1/3; RFC §4.12). Step 2 Slice 1 (metadata), Slice 2+4 (content, scans and images) and Search v2 are live (2026-10-14);
§26 (card accuracy, capture, inaccessible files) was deployed on 2026-10-19 (PRs #81–#84); one extraction path (Slice 3), long audio/video transcripts and full editing are not.
- **Deep research copies (opt-in, 2026-10-14).** The Cabinet switch "Copy deep research reports to my drive"
  (`UserBotConfig.drive_copy_deep_research`, default off, `/api/drive/deep-research-copy`) is the consent. Only deep
  research reports are kept (no morning issues or email reviews; the media bucket's TTL is unchanged). Every deep
  research path ends in `deliver_deep_research`, which marks the HtmlPageGenerator task (`deep_research_report_query`);
  after that HTML's link is delivered, `AgentWorkerHandler` → `DeepResearchDriveCopyService` saves the same HTML via
  `UserDriveService.save` into a UI-language folder ("Deep research", "Глибокі дослідження", …) as
  `YYYY-MM-DD <title>.html`, and posts one line: copied / not connected / failed. Zero-LLM, no retry, never fails the
  delivery; with indexing on the copy is indexed like any HTML. See `decisions/deep_research_drive_copy.md`.

**User Drive indexing** (`docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md`: Slice 1 finds a file by metadata, its deltas
from the RFC text are §23, cited in code as `RFC Δn`; Slice 2+4 answers from content, §24; **Search v2**, §25, D14–D31;
`docs/05_building_blocks/user_drive/README.md`, the owner's map — keep it current) —
**opt-in** (`UserBotConfig.drive_indexing_enabled`, Cabinet toggle, `PUT /api/drive/indexing`). A disposable mirror of
the App Folder: `{prefix}drive_index_records` (one record per item, doc id `{user_id}:{item_id}`, 768-dim
`card_vector`, **no stored path** — paths come from the `parent_id` chain at read time; `card_capped`/`card_refused`
are flags beside the structural state), `{prefix}drive_index_chunks`, `{prefix}drive_index_hints` (v2) and
`{prefix}drive_sync_state` (one per user: delta cursor bound to its drive, epoch, lease, `access_lost_at`, `purging`,
`applied_space`, `applied_versions`). **Lease-fenced:** a tick writes nothing past `lease_until - LEASE_MARGIN`, and
state save/delete are transactions by owner (`LeaseLost` = quiet stop). Pure domain decides everything
(`drive_index_planner.py`, `drive_index_policy.py`, `drive_card.py`, `drive_keys.py`, `drive_enrich_plan.py`,
`drive_tiers.py`); `DriveIndexingService.tick`: gate → lease → observe (nudged items, then delta) → work off due records
by class (edits, cascades, first-seen and re-listings, maintenance, retries) → upgrade pass → release, then
continuation. Errors are classified by the call (RFC §8.2): item calls defer that item; the feed, token endpoint and
embeddings are systemic (only an HTTP 400 card refusal backs one card off). **A third Cloud Run service:** the tick runs
only on `alek-bot-indexer-dev` (`indexer_main.py`, `drive_sync`-only `/worker`, `--ingress=internal` + OIDC with a
pinned audience, gen2, 2 GiB, concurrency 1, max 1; logs `make fetch-logs-indexer`, no Logfire), fed by queue
`drive-sync-dev` (`max-concurrent-dispatches=1`). URL chain `INDEXER_URL_DEV` → `_INDEXER_URL` → `DRIVE_INDEXER_URL`
(`os.getenv`); `make deploy` refuses it empty unless `DRIVE_FIRST_PASS=1` (`docs/07_deployment/README.md`). The main
service fans out `start_drive_sync` every 10 min (`DriveSyncSchedulerService`: minimal sync state for enabled users,
staleness alert, one reconnect notice; v2 adds the awaiting-rebuild, stalled-rebuild and fallback-share alerts) and
nudges after each Step 1 mutation. OS/lock artefacts get no card; dot-folders, `.git`/`pyvenv.cfg` folders,
`node_modules`/`__pycache__`/`site-packages` and dense trees collapse to one card. **Content (Slice 2+4, §24; live since 2026-10-14, PR #60):** the
§5.1 document kinds are parsed on the indexer by a child process (`SubprocessContentExtractor`, rlimits, run as the
unprivileged `driveparser` user — it cannot read the indexer's keys or rewrite its code, but can reach the metadata
server and the network: owner-accepted residual, `decisions/drive_parser_sandbox_resource_isolation.md`); PDFs the
parser cannot read (no text, low density, garbage, failure — never after a timeout/memory kill) and images triaged as
text go to the reader agent (`drive_reader`, 10-page windows); every image is triaged (`drive_triage`, 800 px
thumbnails). Model agents run on GPT-6 Luna by default, Claude by `agent_providers`, then the global
`provider_preference`, and fail over to Gemini Flash (`gemini-3.8-flash`, `fallback_tier`), billed to the user at the
model that ran; a model outage defers only model work, except a reader 5xx/timeout/unexpected error, which is charged
to the file (`failed` at 3); an edited reader-read file is re-read at most once per 24 h; card headroom is decided
before any content work. Text passes a machine-text gate, is chunked (generation-fenced) and embedded. Audio and video
are metadata-only unless the user's transcription is on (next paragraph). No rollback to the Slice 1 revision.

**Transcripts of recordings (RFC §27; T1 live since 2026-10-21 for the owner only, cap lowered to $10 by `DRIVE_TRANSCRIBE_USD_CAP_DEV` under the code's $20; the mirror folder T2 live since 2026-10-21 (owner); the summary T3 is not built).**
Audio and video are `Admission.TRANSCRIBE` (`POLICY_VERSION` 3; structure decisions compare `STRUCTURE_POLICY_SINCE` = 2, so the bump redoes no folder work).
Not an extractor chain link: a **job with its own states on the record** (`transcript_state`: `none ready working waiting current held parked failed`), advanced by
`DriveTranscriptStage` on the indexer across ticks — probe the Graph download URL, cut audio-only Opus windows (≈ 30 min, 60 s overlap) with ffmpeg in the
parser sandbox, upload to the Gemini Files API, one **Gemini Batch** per user per step (half price), poll, collect, stitch and align speakers by code, then
index the transcript through the ordinary text path and store it under the content key (a copy or move never pays again). **Two switches, both off by default:**
the indexer's kill switch `DRIVE_TRANSCRIBE_ENABLED` (a Cloud Build substitution from the deploy-only `DRIVE_TRANSCRIBE_ENABLED_DEV`) and the user's
`UserBotConfig.drive_transcribe_enabled` (owner's script; only the owner, until the zero-credential parser service exists). **Hard monthly cap**
(`transcribe` class, `TRANSCRIBE_USD_PER_MONTH` $20, no initial-build share): a whole file's estimate is **reserved** before its first window and settled at
collection in the reservation's month; a file that does not fit is parked whole until next month; an optional `DRIVE_TRANSCRIBE_USD_CAP_DEV` can only lower the
cap. Billing keeps the held `gemini-3.8-flash` price (≈ $0.16/audio hour, ≈ 125 h/month until 2027). No `prompt_content`/Logfire row per window (a batch is not a
call). Opening a transcribed recording returns the transcript (`get_recording_link` → a link). Rollout: the rollout notes (ops); levers: `docs/07_deployment/README.md` →
"Standing levers"; the owner's map `docs/05_building_blocks/user_drive/README.md` §4.23; decision `decisions/drive_transcripts_t1.md`.
**Mirror folder T2 (RFC §27.9, Δ183–Δ244; live since 2026-10-21, owner).** The indexer writes each transcript as `<name>.md` into a bot-owned
**`_Transcripts`** folder at the App Folder root (D49: the same name in every language; fixed `_README.md`, `Archive`, `Long paths`), reconciled by `DriveTranscriptMirror`
(ports only) as the **last step of `_work_off`**, with its own op/read/stamp budgets and a 45 s reserved tail. State lives in two collections **no purge deletes**:
`{prefix}drive_mirror_roots` (which folder is the bot's, cursors, holds, loss marks) and the ledger `{prefix}drive_transcript_mirror` (key has no drive id; reads filter `drive_id`). **D48:** a deleted
recording's file moves, after a complete round and 1 h, to `_Transcripts/Archive/<day>/`; the bot never deletes it, its store entry stays pinned
(`delete_user_transcripts_except` keeps pinned keys in every purge), and only the deleted-files search (`search_deleted_recordings_and_notes`) reaches it (ledger-backed, works with drive search off). **Owner ruling 2026-10-21 (Δ241):** every `.md` in `_Transcripts`, active or archived, belongs to the bot and is overwritten by it (a rename or move inside is moved back; a custom version = copy elsewhere; deleting an archived file = forget, moving it out = the owner's copy; detached files keep a pinned copy and are overwritten the same way, Δ243). No mirror-only off switch: the stop lever is the user's flag or the kill switch, which also stops transcription.
**OQ-T10:** an indexing opt-out deletes nothing on the drive. Writes use `conflictBehavior=fail` with an intent recorded first and are checked to end under the current root; chat drive
writes into the folder are refused (`UserDriveService` guard). Unexpected errors are charged per file and park it. **Deploy gate: the Δ217 composite indexes must be `READY`** before the
T2 revision ships (a missing one leaves the owner's mirror idle; the tick goes on, Δ236, and no purge waits on one, Δ237). Rollback needs the queue paused and `_Transcripts` moved out of the App Folder. Scripts: `drive_transcript_sweep.py`
keeps the mirror's data unless `--mirror` (`--mirror --apply` also needs `--after-rollback` and `_Transcripts` already out of the App Folder); `drive_mirror_status.py` is read-only (output to `scripts/memory/`). Decision `decisions/drive_transcripts_mirror_archive.md`.
**Search v2 (live since 2026-10-14, PR #64):** intelligence at
index time, zero-LLM search. The indexer describes each text file once (`drive_profiler`: type and life areas from
fixed enums, dates, entities, values, topics, en/es/uk/ru word lists, verified against the text), writes passage hints
per chunk (`drive_passages`: doc2query keywords and questions grounded by an evidence quote), and gives each photo an
enriched description (triage v2, batches of 8) — in the tick's enrichment share (≥ 40 % of the start window), with
per-job states, a newest-first queue and per-agent backoff. Budgets are dollars per class per month (reader $4, triage
$3, profile $4, passages $20) in `{prefix}drive_model_usage` (kept by a purge) after a one-time $40 initial build.
One normaliser (`drive_keys.py`) keys words as form sets (`w:`), one transliteration skeleton per word (`x:`/`xl:`,
translation only), number terms (`n:`), entities (`e:` printed / `ef:` index spelling), D31 label names (`lw:`/`dw:`)
and automatic words; a `KEYS_VERSION` bump re-keys from stored text. The specialist's `search_drive` →
`DriveSearchService`: per query word, about / name / word / hint key streams; strict AND (an absent word blocks, with
the D29 "Without X: N files" line; one lowest-idf word and >30 % common words may be missed, numbers never); files in
tiers About / Mentioned in (exact), By translation / Labelled (automatic), Related by meaning only (≤ 3, relative
z-floor `Z_RELATED` 2.5, uncalibrated); a coverage line names what was not searched or described; results in a
nonce-tagged `<drive_document>` wrapper; reads only the tool's `query` (a time span goes in the date fields). A sync state
built by v1 (no `keys` in `applied_versions`) is gated (D30): no tick work, search answers "being rebuilt"; the rollout
rebuilds every user (rollout notes, ops). The former `drive-search` skill (D32) is split by who
acts on it (DRIVE_SPECIALIST_RFC §5.7): the search mechanics are the specialist's token `PROTOCOL_DRIVE_SEARCH`; the
rules Smart needs to answer (tiers, find vs value, the 90 % rule, versions, document vs memory) reach Smart as reading
lines code selects from the blocks (`src/domain/drive_answer_texts.py`, the `R_*` texts, chosen by
`drive_answer.reading_lines`: changing one is a code deploy, while `PROTOCOL_DRIVE_SEARCH` is a Firestore upload); the `drive_specialist` block of `PROTOCOL_AGENT_SELECTION` keeps the intent, when
to use the drive and the untrusted-text line, and `query_language` exempts the words to find. Not in
`SearchEnrichmentService`. **Files the bot cannot open** (RFC §26.2, D33; deployed 2026-10-19, PRs #81–#84): an item with its own permission list lacking the app's entry is listed by delta but 404s on every id call, so
`get_change` raises on a 404 (never a deletion) and the tick marks the record `inaccessible_since` (card and readable
generation kept, no content work), re-reads it by id on an observation (≤ 1/h) or every 7 days, and only a tombstone
deletes it; search shows an access line, list/open answer from the index (`access_note`), and the main service's fan-out
sends one localized notice with the paths as an attached file (`inaccessible_notified_since` is fan-out-only, never in
the tick's record writes). **Capture, accuracy, scene labels** (RFC §26.3–§26.13, D34–D40; same deploy): every text-bearing
file records how it was made (`capture`: scan / document photo / screenshot / digital, decided by code from the PDF's
structure where it can, else by the model; keyed `cp:`/`cpa:`); each profiled card carries one `card accuracy: N%`
(values graded `in_text` / `in_text_as` / `not_in_text`, nothing dropped, score never in the card text), and the 90 %
rule (open the file for a value below it, never for a find) is in `PROTOCOL_DRIVE_SEARCH` and in Smart's reading line; triage gets the
photo's place and coordinates rounded to 3 decimals as a prior (D39) and returns `scene_labels` from a fixed 32 (`sl:`,
D40). The migration gate key is now `accuracy`; the rebuild runs with no initial-build reset, under the monthly caps. Opt-out purges at once;
lost access keeps the index 30 days. Rebuild one user: `scripts/infrastructure/drive_index_rebuild.py`. Evals:
`python -m scripts.validation.drive_eval` (G1/G2, v1 bars), `… drive_eval.search_v2_eval` (v2 families, offline by
default, `bars_v2.json`; results RFC §25.7.1), G3 `… drive_eval.smart_dry_run` (owner-run, paid, `--max-usd` required, a hard stop; bars in `bars_g3.json`, not recorded yet, so a run without `--record` ends "no bars"; drive writes stubbed by the class-level `dry_run_drive.DryRunDriveOperations`; `--prompts-from-snapshot`, `--specialist-provider`, `--specialist-tier` run the branch's prompt files and pick the specialist; `--record` writes no bars while Smart fails a hard check). **Before/after:** before = G3 on `main` with the Firestore prompts, after = G3 on the branch with `--prompts-from-snapshot`, both at the same `--repeats`; compare the two reports `scripts/memory/drive_g3/g3_<stamp>.json` (`scores`, `wall_p50_s`). The drive specialist eval `… drive_eval.specialist_ab` (owner-run, paid, `--max-usd`; `--replay-commissions` runs the specialist alone; `--compare <report> --compare-design today`; `--prompts-from-snapshot` runs the branch's prompt files). Decisions:
`decisions/drive_*.md`.

**Owner notes on drive files** (`docs/10_rfcs/DRIVE_FILE_NOTES_RFC.md` rev 3.3, D1–D21; built on `feat/drive-notes`, not
deployed; the owner's map `docs/05_building_blocks/user_drive/README.md` §4.24; `decisions/drive_file_notes.md`) — what the
owner says or confirms a file or folder is, kept bot-side in `{prefix}drive_notes` (SCD2 `versions/v<n>`; never on the drive,
never in memory, never purged with the index). **Written only inside the drive specialist:** Smart has no note tool and puts
a note's words in the `work_with_drive_files` query (`file_contents` is "never a note's words"). `DriveAgent` declares
`get_note` whenever `DriveNoteService` is wired, `DRIVE_NOTES_ENABLED` is on and there is a user, and the ten writers
(`create_note`, `add_to_note`, `replace_note_text`, `set_note_type`, `clear_note_type`, `add_life_areas`,
`remove_life_areas`, `delete_note`, `undo_note`, `erase_note`) only on an owner turn (`is_owner_turn`: `interactive_delivery`,
or `run_origin` exactly `voice:ask_alek` / `voice:tell_alek`), re-checked per call; rules in the non-overridable token
`PROTOCOL_DRIVE_NOTES`. No tool replaces more than its name says; a change that would empty a note is refused;
`replace_note_text` needs a `get_note` read in the same run (`CHANGED_SINCE_READ`). One write loop in the core
(`_write(compose)`: composed inside the conflict retry, identical = no-op, append and undo re-run safe); writes run under
`asyncio.shield` in a task `DriveOperations.keep_alive` holds; ≤ 10 per specialist run. The receipt (stored words verbatim,
sent literally) and `[Drive note: <path> ref=… v<n>]` ride the specialist's pass-through collector, so a cut run keeps them;
a write that lands after the answer posts its receipt late (`notify_drive_note_receipt`); on a call the Alek gateway posts
it. The indexer merges a copy into the card (`noted by the owner: …`, About via `AboutField.OWNER_NOTE`, no version bump);
a confirmed-gone file's note is archived and found only by `search_deleted_recordings_and_notes` (`drive_note:` ref).
Erase needs a code (`$drive-notes erase <code>`, `$drive-notes erase-all`). `DRIVE_NOTES_ENABLED` (`os.getenv`, default on;
deploy-only `DRIVE_NOTES_ENABLED_DEV` → both services) off: no note tool, no command, no indexer notes work; reads go on.

**Per-channel sessions** — `session_id = f"{user_id}:{channel_id}"`, deterministic; each channel (Slack
C.../D..., Telegram chat_id) has its own session/history/consolidation stream (a DM is just channel D...).
Adapters resolve session_id synchronously. Async delivery uses `origin_channel_id` from message context
(propagated via DelegationEngine passthrough); system notifications go to primary channel. `GcpTaskQueue`
uses `_DomainEncoder` for transparent Pydantic, datetime and enum serialization in Cloud Task payloads.

**Multilingual Support** — two independent axes: (1) **response language** via prompt tokens
(`LANG_MIRROR` default = mirror input; `LANG_FIXED_{UK,EN,FR,ES}` = fixed); `LanguagePreferenceService`
swaps the token atomically + injects a system alert so the LLM adopts it immediately. (2) **UI language**
(status messages, prompts) via `LocalizationPort` → `FileLocalizationAdapter` (`src/locales/{code}.py`),
resolution USER → ACCOUNT → SYSTEM. Domain: `LanguageCode` enum (uk/en/fr/es). Adding a language +
Cabinet `/api/user/language`: see `docs/10_rfcs/MULTILINGUAL_SUPPORT_RFC.md`.

**Memory search** — parallel queries across different vectors, ranked via Reciprocal Rank Fusion (RRF).
The Router does one prefetch per message (up to 4 vector channels + the domain channel), shared by the
orchestrator turn; `search_memory` searches again with up to 7 channels.

## Economics

- Cost optimization is complexity-driven tier selection within Smart: simple requests resolve to a
  cheaper tier model (ECO/BALANCED), expensive models reserved for complex requests. (Earlier this
  was a Quick-vs-Smart path split; primary routing is now Smart-only — see Multi-agent network above.)
- 1 vCPU Cloud Run — async is mandatory. The ~$100/month budget cap was **dropped 2026-07-26**
  (owner decision): report cost when asked, but do not treat it as a binding design constraint.
  The `daily_cost_limit` alert is advisory and never gates a request.
- **Prices change under you.** OpenAI cut GPT-5.6 by up to 80% on 2026-07-30, three weeks after
  GA. `make check-pricing` audits `billing.py` against two catalogs, but they lag a fresh
  announcement by days — a dated `PRICE_SCHEDULE` entry outranks them. Act on `consensus_differs`
  / `schedule_drift`; everything else is a lead to verify at the provider. See
  `decisions/openai_gpt56_price_cut.md`.
- Solo-dev — maintainability beats architectural elegance
- **Smart cost sweet spot:** a small model with reasoning effort `medium` matches flagships on Smart's
  multi-step delegation (eval 2026-04-12, gpt-5.4-mini). As of 2026-10-03 that model is **`gpt-6-luna`**
  (OpenAI BALANCED): it beat gpt-5.6-luna 7:1 on a dry-run Smart A/B at 37% lower cost
  (`scripts/validation/ab_agent_models.py`, `decisions/model_refresh_2026_10.md`). Configure via
  `UserBotConfig.agent_thinking={"smart":"medium"}` + provider `openai` at BALANCED.

## Architecture

Hexagonal Architecture (Ports & Adapters).

```
src/
  domain/       — Models, enums, value objects. ZERO external deps (stdlib + pydantic only).
                  Key: llm.py (LLMRequest/LLMResponse/Message/MessagePart/ToolCall, PROMPT_CACHE_BOUNDARY,
                  build_tool_turn), auth.py, retry_policy.py, exceptions.py. MessagePart.consolidation_text
                  is visible only to the consolidation serializer (legacy: no writer left, readers kept until stored sessions drop it).
  ports/        — ~58 ABC interfaces (domain/ + stdlib only). One port per system boundary.
  adapters/     — Port implementations: LLM (Gemini/Claude/Grok/OpenAI), Firestore repos, Slack/Telegram,
                  Gmail, MicrosoftToDo (GoogleTasks frozen), Node runners (DOCX/Puppeteer), Unsplash, MCP repo.
                  No cross-subpackage adapter imports (REQ-ARCH-23).
  services/     — Business logic; ports via DI. No concrete-adapter / cross-service imports (REQ-ARCH-22).
                  Incl. prompt_builder.py (PromptBuilder + UserPromptBuilder), search enrichment, email
                  (indexing/search/repair/notification), tasks (indexing/setup), MCPAuthorizationService.
  agents/       — Multi-agent system (inherit BaseAgent). core/ — orchestrators; rest — specialists (roster + patterns in src/agents/CLAUDE.md).
  handlers/     — Entry points: ConversationHandler, WorkerHandler (dispatches /worker Cloud Tasks
                  by task_type), AgentWorkerHandler (task_type="agent_execution" — async agent runs
                  + deep-research delivery).
  infrastructure/ — AgentCoordinator, queues, agent_config.py, agent_registry.py (AgentDescriptor + registry),
                  agent_manifest.py (Intent constants + all agent declarations — single source of truth),
                  delegation_engine.py (reusable multi-turn tool loop).
  composition/  — ServiceContainer + UserAgentFactory (AgentFactoryPort; lives HERE, not services/) +
                  application_graph.py (`build_application_graph`: every non-web object the main service runs,
                  shared by `main()` and the tests) + adapter factories + MCP wiring (mcp_setup.py,
                  mcp_sdk_oauth_provider.py). The only layer allowed to cross all boundaries.
  locales/      — Per-language UI strings (uk/en/fr/es.py), loaded by FileLocalizationAdapter.
  config/       — EnvironmentConfig, Settings, AuthConfig.   utils/ — logger, telemetry, debug_logger, file_conversion.
  web/          — Quart app (OAuth + Cabinet UI + MCP consent). Endpoint families: /auth/* (login + connect
                  gmail/google-tasks/microsoft-todo), /api/gmail/*, /api/tasks/*, /api/user/* (facts, timezone,
                  location, language, reminders, deep-research, platforms/invites), /webhook/microsoft-tasks/<id>,
                  /cabinet*, /mcp/consent, deep_research_webhooks, /health.
main.py         — Bootstrap: telemetry, settings, the Firestore client, `build_application_graph` (composition/),
                  then the web layer: blueprints, voice, remote MCP server (dedicated SearchEnrichmentService,
                  build_mcp_components, ASGI dispatcher via hypercorn), `/worker` + graceful shutdown.
sandbox_main.py — Untrusted-content sandbox entry point (supervisor + `--serve`), its own Cloud Run service,
                  image `Dockerfile.sandbox`; imports nothing from infrastructure/agents/services/handlers (image closure).
docx_generator/ — Node.js project (docx npm lib); NodeDocxRunner writes temp scripts here. Not a Python package.
```

## File Size Convention

One public class per file. If a file contains more than one public class — split it.
File length is not a constraint; mixed responsibilities are.

For files over ~600 lines: put the public interface (class + method signatures with docstrings)
at the top of the file, implementation below. This allows reading the contract in the first
50 lines without scrolling through the implementation.

## Layer Semantics (when in doubt, use this to decide where a new class goes)

- **`domain/`** — pure data and algorithms. No I/O, no side effects, no logging. If a class only
  needs stdlib + pydantic, it belongs here regardless of how complex the logic is.
- **`ports/`** — contracts (ABC). One port per system boundary. Do not create a port for a
  single internal service with no substitution need.
- **`services/`** — orchestrate I/O through ports. Do NOT participate in agent routing.
  Do NOT inherit BaseAgent. If it takes ports via constructor and coordinates work — it's a service.
- **`agents/`** — inherit BaseAgent, receive AgentMessage, return AgentResponse. Participate in
  multi-agent routing via AgentCoordinator. If it doesn't extend BaseAgent — it's not an agent.
- **`handlers/`** — entry points for external events: HTTP request (Slack/Telegram webhook),
  Cloud Task dispatch, Cloud Scheduler trigger. Three handler classes exist (Conversation,
  Worker, AgentWorker). A new one only if a new external event source is added — not because
  a class is large.
- **`composition/`** — wiring layer. The only layer allowed to know about all other layers
  simultaneously. ServiceContainer + factories live here. If constructing an object requires
  importing from 2+ concrete layers — it belongs in composition/.

## Import Rules (CRITICAL)

```
domain/   → ONLY stdlib, pydantic. Never adapters/, services/, config/.
ports/    → domain/ + stdlib + ABC.
adapters/ → domain/, ports/, config/. No cross-subpackage adapter imports (REQ-ARCH-23).
services/ → domain/, ports/. Do NOT import concrete adapters or other services (REQ-ARCH-22).
            Cross-service deps use TYPE_CHECKING guards or constructor injection.
agents/   → Inherit BaseAgent. Receive dependencies via constructor.
```

## Code Conventions

- **All I/O — async/await.** No synchronous calls to DB or LLM.
- **Pydantic BaseModel** for domain entities. **@dataclass** for value objects (MessageContext, RoutingMetadata).
- **File naming:** `{entity}_service.py`, `{provider}_adapter.py`, `firestore_{entity}_repo.py`, `{purpose}_agent.py`.
- **Class naming:** `GeminiAdapter(LLMPort)`, `FirestoreFactRepository(FactRepository)`, `QuickResponseAgent(BaseAgent)`.
- **Shared state** protect with `asyncio.Lock`. No exceptions.
- **Errors** log before re-raise. Do not silently swallow exceptions.
- **Do not use print()** — only `from src.utils.logger import logger`.
- **No fallback prompts.** Agents must not contain inline/hardcoded fallback prompts.
  If `PromptBuilder.build_for_agent()` fails — return `AgentResponse.failure()`, do not
  degrade to an empty or inline prompt. The Firestore prompt (token + blueprint + profile)
  is the single source of truth. Fail fast on missing prompts.

## Patterns

- **Port is justified** when: 2+ implementations, testable substitution, system boundary.
- **Port is not needed** for internal services with a single implementation.
- **Adapter / provider patterns** — PerformanceTier tier→model traps, ProviderRegistry, adapter
  capability gates, GeminiEmbeddingAdapter, PromptCacheStrategy → moved to
  [`src/adapters/CLAUDE.md`](src/adapters/CLAUDE.md) (loads when working in `adapters/`).
- **Agent-orchestration patterns** — AgentDescriptor, Intent, specialist delegation (commissioning
  model, `query` vs `context`), DelegationEngine (multi-turn loop, context passthrough, `deliver_response`
  terminal tool — live on Grok (the response schema as the tool's parameters) and on Claude (one `response`
  string, schema from the prompt, for schema + client-tool requests); Smart, Quick and Tutor pass it; cold on
  Gemini/OpenAI; co-emitted siblings are dispatched before the
  terminal return; TD-3 closed as invalid, do not delete it — intent fan-out),
  BaseAgent lifecycle hooks, `build_tool_turn`, LLM-content capture,
  CircuitBreaker, transcript integrity, AgentConfig → moved to
  [`src/agents/CLAUDE.md`](src/agents/CLAUDE.md) → Orchestration Patterns (loads when working in `agents/`).
- **SCD2 versioning** — a content change is a new `FactEntity` document linked by `lineage_id`, `version`, `replaces_fact_id` and `superseded_by` (`domain/fact_lifecycle.py`: forward resolution, in-place fields); `is_current` = latest of its lineage, `state` is the only visibility switch.
- **Multi-tenant** — always pass account_id. Collections with env prefix.

## Adding a New Specialist Agent

See [`docs/how_to/NEW_AGENT_PLAYBOOK.md`](docs/how_to/NEW_AGENT_PLAYBOOK.md) — **mandatory protocol**.

When adding a new agent or capability, also update [`src/utils/capabilities.py`](src/utils/capabilities.py) —
this file is the user-facing capabilities reference returned by the `get_help` intent.
Read Phase 0 before writing any code. Follow steps in order. Do not skip.

## Adding or Modifying an LLM Adapter

See [`docs/how_to/ADAPTER_WIRE_TESTING.md`](docs/how_to/ADAPTER_WIRE_TESTING.md) — **mandatory protocol**.
Every new or modified adapter must have wire tests (mock at SDK boundary, not port) and
contract validators in `tests/contracts/adapter_contracts.py`. Never mock at the port level
in adapter tests — that pattern cannot detect translation regressions.

## Agent Output Format Standards

**Every agent with structured LLM output MUST follow the output-format standard.** The full standard
lives in [`src/adapters/CLAUDE.md`](src/adapters/CLAUDE.md) → Agent Output Format Standards — it sits
next to the adapter code that enforces it and loads when working in `adapters/`. It covers: mandatory
`OUTPUT_FORMAT_{AGENT}` token; `_parse_response()` → `json.loads` with **no regex fallback**;
retry-on-invalid (not silent degradation); the three provider-specific JSON-enforcement mechanisms
(`response_mime_type`, `response_schema`, OUTPUT_FORMAT token) incl. the two Claude paths
(`output_config.format` without tools; a string `deliver_response` with tools); `_RESPONSE_SCHEMA` on Quick/Smart; and the `rich_content.data.rows` `[{cells:[…]}]` shape.

## Tests

- pytest + pytest-asyncio (asyncio_mode=auto).
- Fixtures in `tests/conftest.py`: `mock_env_config`, `mock_llm_port`, `mock_repository`.
- Fakes with semantics over call assertions; mocks only for boundaries you do not own (`tests/CLAUDE.md`).
- Markers: `@pytest.mark.requirement("REQ-XXX")`, `@pytest.mark.performance`.
- Structure: `tests/unit/`, `tests/integration/`, `tests/performance/`, `tests/support/`, `tests/plugins/`, `tests/contracts/`.
- **Adapter wire tests:** `tests/unit/adapters/` — mock at SDK boundary (not port). See `ADAPTER_WIRE_TESTING.md`.
- **Contract repository:** `tests/contracts/adapter_contracts.py` — named `ContractRule` objects with per-provider validators. Reused by both unit and integration tests.
- **Integration layer:** `tests/integration/adapters/` — `CapturingStub` + contract validation. Run with `pytest tests/integration/adapters/ -v`.

## Decision-Making Protocol (CRITICAL — apply before every non-trivial task)

Every implementation decision must pass through four sequential gates.
Skip any gate only when the task is unambiguous, isolated, and trivial (typo / rename / single line).

### Gate 1 — Orient: find the authoritative source

```
Is there a relevant RFC in docs/10_rfcs/?
  YES → Read it fully before writing a single line of code.
        Does the RFC reference a POC script in scripts/?
          YES → Read the POC fully. The POC is the authoritative implementation spec.
                POC = ground truth. It encodes validated, debugged logic.
                Only POCs explicitly referenced from an RFC qualify as authoritative.
          NO  → RFC alone is the spec.
  NO  → Existing production code is the spec. Read it before proposing changes.
```

### Gate 2 — Gap analysis: compare intent vs. reality

Before writing code, explicitly answer:
1. What exactly does the RFC/POC prescribe for this step?
2. What am I about to implement?
3. Is there any difference? (missing filter, different algorithm, different data structure, altered flow)

If there is ANY difference → do not proceed to Gate 3. Go to Gate 4 first.

### Gate 3 — Uncertainty check: stop or go?

Ask yourself: "Am I fully certain about every detail of this implementation?"

Signals that mean STOP and ask:
- The RFC/POC covers this case but my reading is ambiguous
- I found a "simpler" approach than what the POC uses — this is a red flag, not a win
- I am about to make an assumption about a parameter, a filter, a threshold, or a flow
- The implementation touches more than one subsystem and I haven't read all relevant code
- Something feels "obvious" but I haven't verified it against the source

Asking questions is efficient. One clarifying question costs 30 seconds.
A wrong autonomous assumption costs hours of debugging and rework.

### Gate 4 — Explicit delta declaration

If your implementation differs from the RFC/POC in any way:
- State the difference explicitly before writing any code
- Explain the reason
- Wait for user confirmation

Do NOT implement first and explain later. Do NOT silently simplify.
Autonomous decisions that diverge from the spec without notification are bugs in the process,
regardless of whether the code itself works.

---

## Project Documentation

Detailed docs in `docs/` (arc42). Read as needed:
- Architecture: `docs/04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md`
- Structure: `docs/04_solution_strategy/current_implementation/STRUCTURE.md`
- RFCs: `docs/10_rfcs/`
- Risks and technical debt: `docs/12_risks/IMPLEMENTATION_ROADMAP.md`

**Where to write records:** multi-step refactors with a migration plan → `docs/10_rfcs/`;
single backward-looking decision records (50–150 lines: decision + alternatives + why) →
`docs/04_solution_strategy/decisions/`. Do NOT add records to `docs/09_decisions/` (arc42 §9): it
keeps only the older ADR-004–ADR-008, whose decisions still shape the code.

## Language

- Respond to the user in whatever language they write in.
- All changes to documents (docs/, CLAUDE.md, code comments, docstrings, log messages) must be written in English.

## ⛔ SECRETS RULE — READ BEFORE TOUCHING ANY FILE

**NEVER write secrets, credentials, infrastructure details, or PII into any git-tracked file.**

This includes — but is not limited to:
- API keys, tokens, passwords, signing secrets, webhook secrets
- Cloud Run service URLs, project IDs, service account emails
- Internal hostnames, IP addresses, resource names
- User IDs, account IDs, email addresses
- OAuth client IDs/secrets, Firebase config values

**The only place for this data is `.env` (gitignored) or GCP Secret Manager.**

If a Makefile target, script, or config needs a URL or ID — define it as a variable
loaded from `.env`, never hardcoded in the tracked file itself.

When in doubt: if it identifies or grants access to infrastructure, it goes in `.env`.

## ⛔ PERSONAL DATA RULE

**The repo is public. Every example in docs, tests, fixtures, prompts, comments and commit messages is invented**,
taken from the owner's invented universe (rule in [`tests/CLAUDE.md`](tests/CLAUDE.md) § Invented cast). Never take an example from the
owner's memory, `get_user_context`, journals or chat history: no real names, relatives, health values, home, legal
status, documents or contact data. A gate checks every commit and PR (`scripts/pii_gate/`, `.githooks/`,
`.github/workflows/pii_gate.yml`); its private denylist and profile never enter this repo. CI runs the denylist only and prints counts; the model check is local-only.
**Before every push** (any branch: a pushed branch is public) run the local semantic personal-data review described
in the session setup; its findings stay local, and nothing is pushed until it is clean.

---

## ⛔⛔⛔ Tests — REVIEWER RULE — READ BEFORE TOUCHING ANY TEST FILE ⛔⛔⛔

**The one who writes the code never edits an existing test to make that code pass. A reviewer
decides each edit.** Owner's standing rule since 2026-09-30, replacing per-test owner approval.

If a code change makes an existing test fail:
1. STOP. The implementer does not touch the test (except the mechanical commit of R7 below).
2. Record EXACTLY which test failed and WHY (assertion, actual vs expected).
3. Hand it to a **reviewer**: the task reviewer in subagent-driven work, otherwise a dedicated
   reviewer subagent. Give it the failing test, the intended change and its spec (RFC, plan).
4. The reviewer decides per test:
   - **The test encodes behaviour the spec intentionally changes.** The reviewer edits it and
     justifies that one test in its report.
   - **Otherwise the code is wrong.** The implementer fixes the code.
5. The owner is not asked. A test is never weakened to make code pass: no deleted assertions, no
   loosened checks, no skips.

New behaviour gets NEW tests. Self-authorized without review: fixing a broken import path caused
by a module rename you just performed.

Every other mechanical adaptation (a rename, a signature or constructor-argument change) goes in the implementer's own commit titled
`test(mechanical): ...`; the reviewer reads that commit as a whole and checks one thing: no assertion changed. An assertion is changed when
a different value, condition or count is asserted; renaming an identifier or keyword inside an assert after a rename is mechanical.
Behavioural edits stay per test under the reviewer rule (`tests/CLAUDE.md`).

This applies to: test files (`tests/`), conftest.py, shared test helpers, fixtures.

Rationale: tests are the specification. Modifying them to make code pass destroys the specification.
A failing test is signal — not an obstacle to remove. The ~3600+ tests across 233 files (for ~150
source files) are **load-bearing, not over-engineering**: this codebase is AI-pair-programmed, and
tests are the only enforcement that runs faster than generated code. Critical bugs map 1:1 to files
lacking parallel tests. Never propose reducing coverage as "cleanup" or "simplification".

## ⛔⛔⛔ Debugging Cloud Run — MANDATORY PROTOCOL ⛔⛔⛔

**When debugging any issue that manifests in Cloud Run, the FIRST action is ALWAYS to read the
actual data. No theories, no speculation from code alone, no proposed fixes before seeing the
actual error.**

### Step 1 — Pull logs LOCALLY, then grep locally (do NOT loop `gcloud logging read`)

`make fetch-logs [K=300]` writes the last K entries to `alek_debug.log`. **One fetch, then grep
the local file** — far faster and cheaper than repeated remote `gcloud logging read` calls.
- `make logs [K=300]` — quick view of last K entries (no file)
- `make logs-tail` — live tail (`make logs-perf` = perf lines only)
- Cloud Run Jobs (e.g. deep research): `make fetch-logs-job [K]` → `alek_debug_job.log`;
  `make logs-job`; `make logs-execution EXECUTION=<name>`
- Drive indexer (`alek-bot-indexer-dev`, the `[DriveIndex]` tick lines): `make fetch-logs-indexer [K]` →
  `alek_debug_indexer.log`; `make logs-indexer`
- Drive specialist (main service): `make fetch-logs`, then `grep '\[DriveAgent\]\|\[DriveOperations\]' alek_debug.log`
  (one `[DriveAgent] user=<8> status= model= ops= turns= ms=` line per commission; the lines are listed in
  `src/agents/CLAUDE.md` → Drive → Logs)

### Step 2 — Two data sources. Pick by what you need; do NOT conflate them.

| Need | Source | How |
|------|--------|-----|
| Operational events, errors, tracebacks, control flow | **Cloud Logging** | `make fetch-logs` (above) |
| Actual LLM prompt/response **content** + tokens | **BigQuery** `alek_observability_dev.prompt_content` | `bq query` (below) |
| The **cascade** — who delegated to whom, what each agent got and returned, failed calls | **Logfire** | trace tree in the UI, or SQL via the Logfire MCP server |
| **Memory** over time — per process, per work unit, how long, what was open at a spike | **BigQuery** `alek_memory_dev` (log sink, 90 days) | queries in `docs/05_building_blocks/memory_telemetry/README.md`; Cloud Run's own metric only says *that* memory grew |

**Logfire holds prompt/response content too since 2026-07-30** (`LOGFIRE_CAPTURE_CONTENT=true`).
Prefer it whenever the question involves *relationships between calls* — BigQuery cannot express
them: no `parent_span_id`, and the stored `span_id` points at the enclosing span, not the LLM call.
Prefer BigQuery for bulk/historical SQL over turns. See
`decisions/logfire_prompt_content_capture.md`.

**`prompt_content`** — one row per LLM call, 30-day TTL. Columns: `trace_id, span_id, timestamp,
user_id, account_id, agent_id, agent_type, model, provider, turn, request_text, response_text,
tool_calls, prompt_tokens, completion_tokens, total_tokens`. **A failed LLM call has NO row here**
(and no span): both `record_turn` and `_emit_llm_span` sit on the success return path
(`base_agent.py:1181,1185`), so an exception exits before either. Failed calls are inspectable in
**Logfire only**. (An earlier version of this file claimed the opposite — corrected 2026-07-30.)
`trace_id` is stored with a `tr_` prefix, so joining to Logfire needs `SUBSTR` first. Query locally;
for the large multi-line `request_text` use `--format=json` and parse (CSV breaks on embedded
newlines):
```
bq query --project_id=<PROJECT_ID> --use_legacy_sql=false --format=json \
  'SELECT request_text, response_text, tool_calls FROM
   `<PROJECT_ID>.alek_observability_dev.prompt_content`
   WHERE timestamp BETWEEN "<from>" AND "<to>" AND agent_type LIKE "%smart%" ORDER BY turn'
```

### Scope discipline
Investigate the **reported incident's time window**. Do NOT trawl unrelated prior-day errors. If
checking for recurrence, search only the **same error signature** — and if you surface another
error class, either analyze it fully or leave it; no half-analysis.

### "No `gcloud`" ≠ "no access" (remote/web session)
The same data is reachable from Python via `google.cloud.logging` / `google.cloud.bigquery` using
the environment's service-account credentials. A missing CLI does NOT mean the data is unavailable.

**Reading the right source costs 1–2 calls. Wrong theories cost 10+ turns and user patience.**

---

## What NOT to Do

- Do not add DI containers (dependency-injector etc.) — manual DI in `src/composition/` (`application_graph.py`).
- Do not create ports for cleanliness — only when there's a real need.
- Do not commit .env, *-admin-key.json, service-account*.json.
- Do not touch `archive/` — this is deprecated legacy code.
- All PII or sensitive data exports (Firestore queries, user facts, analysis results) MUST be
  saved only to `scripts/memory/` (gitignored). Never save them to tracked directories.
- Firestore uses the `us-production` named database (the `(default)` database is not used).
  Always use `database="us-production"` (or rely on `FIRESTORE_DATABASE` env var). Live data is
  in the `development_`-prefixed collections; the unprefixed prod collections were deleted
  2026-05-31 (see Branching & Environment above).
  Every Firestore client passes `database=` (scripts: `scripts/_firestore.py` or `EnvironmentConfig().firestore_database_id`); an AST test enforces it.
