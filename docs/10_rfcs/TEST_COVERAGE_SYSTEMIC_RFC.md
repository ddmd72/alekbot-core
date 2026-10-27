# RFC: Tests that verify behaviour behind the ports — a system-wide plan

**Status:** Accepted for implementation (owner, 2026-10-21): rev 2, independent review applied; owner decisions §7 and
confirmations at the end. Test infrastructure created 2026-10-21; nothing else built.
**Trigger:** the fact-lifecycle audit (`FACT_LIFECYCLE_RFC.md` §1, Appendix B): eleven defects in memory facts, none
caught by ~16 000 tests, in a subsystem reported as ~100% covered.
**Evidence:** a full unit run with coverage on 2026-10-21 (15 995 passed, 89.6% of 46 534 statements), taken **before
the fact S1 fix merged**; a read-only blind-spot analysis of the fact lifecycle (owner-local report
`scripts/memory/test_blind_spots_report_2026_10_21.md`); two independent reviews of rev 1 (feasibility, and gaps
across the whole system) and a binding arbitration of their findings. Every code reference below was checked against
`main` after S1 merged.

## 1. Problem

### 1.1 The suite measures execution of the caller, not behaviour behind the port

- Before S1, the fact lifecycle's callers were fully covered (`consolidation_agent.py`, `fact_write_service.py` 100%),
  the code that stores facts was not (`fact_management_adapter.py` 15%, `firestore_repo.py` 17%). Replacing 12
  lifecycle methods with functions that raise left the same 486 tests green. S1 added a write→read round-trip suite on
  an evaluating fake (`tests/unit/adapters/test_fact_store_roundtrip.py`, `tests/support/fact_store.py`); the rest of
  the codebase still has the pattern.
- The pattern is written down as the convention. `tests/CLAUDE.md` prescribes `AsyncMock(spec=PortClass)` and
  `assert_called_once()`: a test proves the caller called the port, never what the port did.
- A `MagicMock` Firestore client answers any query with whatever the test canned. Ordering, a missing field, an
  equality filter on an absent field, a projection that drops a field, a filter on the wrong flag: none of these can
  fail on a mock. That is exactly the class of the fact defects (oldest-first domain query, `context` dropped by the
  projection, `is_current` drift, 88 invisible documents).
- **High line coverage on a mock hides the same class.** At least a dozen stores sit at 77–100% on `MagicMock` with no
  evaluating fake in any of their test files: `firestore_ephemeral_store` (the single-use voice ticket),
  `firestore_dedup_store`, `firestore_long_turn_registry`, `firestore_agent_note_adapter` (the reminder delivery stamp),
  `firestore_skill_repository`, `firestore_quota_service`, `firestore_channel_binding_adapter`,
  `firestore_memory_fact_draft_repository`, `firestore_companion_memory_repository`, `firestore_iam_adapter`. Work
  chosen by line coverage would leave all of them open. This plan chooses work by **inventory** (P11).
- The same mock-the-neighbour pattern sits **above** the ports: handlers mock internal services without a spec, so an
  argument that a service ignores unless another one is also set is never noticed (§1.4 item 6).

### 1.2 Where the behaviour behind the ports is untested (coverage before S1, 2026-10-21)

The numbers come from `cov.json` taken before S1 merged; S1 raised the two fact-store files. P2 re-measures with branch
coverage on.

| Layer | Coverage | Statements |
|---|---|---|
| domain | 99.2% | 9 331 |
| infrastructure | 97.3% | 1 319 |
| agents | 96.1% | 5 001 |
| services | 93.9% | 9 664 |
| handlers | 93.2% | 1 794 |
| **adapters** | **80.5%** | 12 564 |
| **web** | **64.5%** | 2 743 |
| root entry points (`main.py`, `relay_main.py`, `indexer_main.py`, `sandbox_main.py`, `job_main.py`) | **not measured** (`.coveragerc` `source = src`) | — |

Inside adapters and web, the low end is the stores and surfaces the product depends on:

| File | Coverage | What it holds |
|---|---|---|
| `firestore_whitelist_repo.py` | **absent from the coverage data** (no unit test imports it) | who may use the bot (both access gates) |
| `firestore_short_link_repository.py`, `firestore_notification_state_adapter.py`, `google_tasks_adapter.py` (frozen), `playwright_html_renderer.py`, `gcp_log_sink.py` | absent | |
| `firestore_invite_code_repo.py` | absent from the unit run; imported only by `tests/integration/test_firestore_invite_code_repo.py` | invite codes |
| `firestore_prompt_repository.py` | 14% | every agent's prompt |
| `fact_management_adapter.py` / `firestore_repo.py` | 15% / 17% (before S1) | long-term memory |
| `firestore_indexed_email_repo.py` | 22% | email memory |
| `gmail_provider_adapter.py` | 28% | inbox access |
| `prompt_v3/firestore_token_repository.py` | 30% | prompt tokens |
| `web/oauth_app.py` / `web/user_cabinet_app.py` | 31% / 38% | sign-in, connect flows, Cabinet API (whitelist gate) |
| `firestore_mcp_client_repository.py` / `firestore_oauth_credentials_adapter.py` | 49% / 51% | OAuth clients and tokens |
| `slack/http_adapter.py` | 56% | Slack ingress |
| `microsoft/todo_adapter.py` | 60% | tasks |
| `firestore_consolidation_queue.py`, `firestore_user_repo.py`, `gcp_task_queue.py` | 62–65% | queues, users |

Seventeen source files that existed then are absent from the coverage data (`telemetry.py` and `server.py` are
omitted by `.coveragerc`). Coverage cannot tell "never imported" from "imported at collection only", so P2 replaces it
with a static check.

The store that works shows the method: `firestore_drive_index_repo.py` 99.9% on an evaluating Firestore fake
(`tests/unit/adapters/firestore_drive_eval_fakes.py`), a parity suite against the in-memory repository
(`test_drive_index_v2_parity.py`) and offline index-provisioning tests (`test_firestore_drive_index_provisioning.py`
and four siblings). The LLM adapters (83–98%) are wire-tested at the SDK boundary (`docs/how_to/ADAPTER_WIRE_TESTING.md`),
which proves translation, not conformance to the provider (§4a).

### 1.3 Other blind-spot classes (each with a live instance)

1. **Prompt ↔ code.** Prompts live in Firestore; nothing checks them against the code. Live: `HISTORICAL` in
   `COGNITIVE_PROCESS_CONSOLIDATION` vs the `ARCHIVAL` enum (`src/domain/entities.py:61`); `metadata` promised on
   `update_fact` and absent from the schema; the Tasks prompt names `task_id` + `list_id`, the schema requires
   `task_ref` (`src/agents/tasks_agent.py:196-299`). No `CONSOLIDATION_*` token is referenced by any test.
2. **Spec ↔ code.** `DELIBERATE_FACT_MANAGEMENT_RFC.md` is "Implemented" in the RFC index and "Proposed" in its header;
   11 of the 15 paths it cites do not exist, including every test file it promised.
3. **Write-only fields.** `ttl_days`, `last_updated`, `valid_to` were written and never read; `expires_at`,
   `replaces_fact_id` never written. Nothing fails when a field loses its last reader.
4. **Configuration drift.** `main.py:106` defaults the database id to Firestore's `(default)` database,
   `src/config/environment.py:72` to the live named database. More than 80 files under `scripts/` build a Firestore
   client and never pass `database=`. The 2026-02-16 migration that should have given 88 legacy facts a `state` opened
   `(default)`.
5. **Tools report success after dropping input** (`update_fact` ignored `metadata` and returned `updated`).
6. **`tests/integration/` mixes three things**: tests on mocks, the offline G2 end to end, and tests that write to
   real Firestore without isolation (`test_sliding_window_e2e.py:84` opens a client on the configured project with no
   `database=`; `test_prompt_4level_e2e.py:158` writes and deletes tokens in the configured database).
7. **Coverage gates exist but do not run.** `make test-coverage` (global 70% + per-file thresholds,
   `scripts/check_coverage_thresholds.py`) is not in CI; only five notification-era files have thresholds; `branch =
   False`.
8. **Redelivery, interleaving and clocks.** Cloud Tasks is at-least-once, but only `agent_execution` claims its task
   (`main.py:464-470`); `daily_email_review` and `execute_reminder` are not safe to run twice (§1.4). The hourly
   daily-review match is DST-blind (§1.4). No suite provokes interleavings, although per-instance accumulators under
   `asyncio.gather` once inflated billing 3.6× (`decisions/billing_execution_scoped_ledger.md`).
9. **Reachability.** `WorkerHandler.handle` dispatches 21 task types (`src/handlers/worker_handler.py:166-212`); in
   the coverage data (before S1), 12 of its dispatch lines never run, including every scheduler-triggered type. An unknown
   `task_type` falls through to the Slack handler, and one handler has no trigger (§1.4).
10. **Data already stored.** B9 (88 facts without `state`) was a data defect left by old code. Fixtures built through
    the pydantic model fill defaults, so a legacy document cannot even be expressed in a test.

**Already guarded, not to be rebuilt:** locale key parity (`tests/unit/test_ui_message_strings.py:36`) and
`config.get` key registration (`tests/unit/test_arch_config_key_registration.py`). They are the precedent for the
meta-tests below.

### 1.4 Defects found by this review

> **Fixed in S1b (2026-10-22), pending deploy:** #1 (expired dedup mark), #2 and #3 (consolidation queue order; cleanup
> removed), #4 (claim covers every delivery; daily review once per local date), #5 (`execute_reminder`: one task per
> fire, and its redelivery is now claimed), #6 (deep-research origin platform), #7 (DST: one review per local date),
> #8 (unknown `task_type` is a 400). #9-#13: fixed on the infra branch of S1b (`fix/s1b-infra-in-git`).

The two reviews found live defects while checking this plan. They are **fixed in a separate urgent session**, not in
this RFC's packages; the packages below add the tests that would have caught each class.

| # | Defect | Where (current `main`) | Effect |
|---|---|---|---|
| 1 | Dedup claim on an expired mark: `create()` raises because the mark still exists, `is_duplicate()` says False because it expired (`:43-44`), and the method returns True **without rewriting the mark** | `src/adapters/firestore_dedup_store.py:63-79` (`try_mark_processed`); used for `agent_execution` claims (`main.py:467`) and Telegram event dedup (`main.py:1116`) | after expiry and before Firestore's TTL purge, every redelivery passes the claim |
| 2 | Consolidation queue order: `get_pending_batches` has `order_by("created_at")` commented out ("we need an index"); the service takes `limit=1`; batch ids are random | `src/adapters/firestore_consolidation_queue.py:45-47`; `src/services/consolidation_service.py:90,155` | a backlog is consolidated in random order. The fix needs a composite index |
| 3 | Consolidation cleanup order: "delete oldest" with its `order_by` commented out | `src/adapters/firestore_consolidation_queue.py:184-196` | deletes in doc-id order, not oldest first |
| 4 | `daily_email_review` has no redelivery claim | `src/handlers/worker_handler.py:946-1016` | a redelivered task runs a second full review and posts a second report |
| 5 | `execute_reminder` is check-then-act: it reads `last_delivered_due`, delivers, then stamps with a plain `update` | `src/handlers/worker_handler.py:800,871`; `src/adapters/firestore_agent_note_adapter.py:305-315` | two overlapping attempts both deliver |
| 6 | Deep-research polling passes `channel_id_override` without `platform_override`; `resolve_channel` honours the override only when both are set | `src/handlers/worker_handler.py:451,475,517,531`; `src/services/user_notification_service.py:119` | the result goes to the primary channel, not the one the user asked in |
| 7 | DST: the hourly daily-review match is `astimezone(user_tz).hour != review_hour` | `src/services/email_review_service.py:56-57` | on the fall-back day (2026-10-25 in Europe/Madrid) the review can be enqueued twice; at spring-forward it can be skipped |
| 8 | Unknown `task_type` falls through to the Slack handler | `main.py:1260-1261` | a typo or a retired task type is handled as a Slack event instead of being refused |
| 9 | `sweep_companion_consolidation` has a handler and no trigger | `src/handlers/worker_handler.py:182,383`; `docs/07_deployment/SCHEDULERS.md:265-272`; `src/infrastructure/agent_manifest.py:714` | stuck companion batches are never re-triggered |
| 10 | `email_indexing_watchdog` is scheduled only by a manual script | `scripts/infrastructure/setup-email-watchdog-scheduler.sh` (absent from the deploy-time scheduler script) | a fresh deploy has no watchdog (resolved 2026-10-22, S1b: the job is in the deploy-time scheduler script, the manual script is retired) |
| 11 | The task-search vector indexes are not defined in git: vector queries with `user_id ==`, `status !=`, `list_id ==` pre-filters, 0 entries in `config/firestore.indexes.json` and none in any index script | `src/adapters/firestore_task_search_index.py:141-152` | a rebuilt database has no index for task search |
| 12 | `create_prod_vector_indexes.py` is stale: an unprefixed collection deleted on 2026-05-31, `is_current`, `EUCLIDEAN`; it provokes a failing query to print a link and creates nothing | `scripts/infrastructure/create_prod_vector_indexes.py:11-12,29-36` | misleading as an index source |
| 13 | Two database defaults: `(default)` in `main.py`, the live named database in `environment.py` | `main.py:106`; `src/config/environment.py:72` | any path that reads the env var without the config object opens the wrong database when the variable is unset |

## 2. Principles (owner, 2026-10-21, and what follows from them)

1. **No test calls a real LLM.** Tests run hundreds of times a session. A model's behaviour is judged only by evals the
   owner approves (cost rules). A test may replay a recorded or scripted model reply (as the drive G2 test does): no
   model runs, no cost.
2. **`make check` stays offline and fast.** Enforced, not hoped: network access is blocked in the unit suite (H3).
3. **Real cloud writes are allowed in a separate gate, if the live data is never touched** (§4).
4. **Every port with a stateful implementation is tested against its semantics, not its calls.** A fake used by
   service tests must behave like the real thing, and that likeness is itself tested (P5, P12).
5. **Every adapter is tested behind its port**, at the boundary where it talks to the SDK, HTTP or the database.
6. **New behaviour = new tests; the reviewer rule for existing tests is unchanged** (CLAUDE.md). Tests that encode a
   defect go to a reviewer.
7. **Coverage that matters is measured where defects live**: per layer with branch coverage, per critical file, by an
   inventory of stateful adapters, and by semantic mutation (a suite that survives broken code verifies nothing). Line
   coverage on a `MagicMock` SDK does not count toward the adapter floor.

## 3. Harness

### H1. A shared evaluating Firestore fake

Start from `tests/support/firestore_eval.py` (S1, on `main`). It subclasses the drive fake
(`tests/unit/adapters/firestore_drive_eval_fakes.py`, 421 lines: filters incl. `in`/`array_contains*`, value-type
ordering with the `__name__` tie-break, cursors, `limit`, `select` projections, cosine `find_nearest` with
`distance_result_field`, `Increment`, `DELETE_FIELD`, aggregations, batches, pass-through transactions) and adds
`find_nearest` without a result field, `distance_threshold`, and `NotFound` on `update` of a missing document. H1 grows
that module, collection-agnostic, by exactly this list:

| Feature | Used at | Fake today |
|---|---|---|
| Dotted field paths, first-class in `update`, `where`, `order_by` and nested reads | `firestore_account_repo.py:81-84` (`"usage.total_tokens": Increment`); `firestore_user_repo.py:66,154` (`platform_identities.{p}`) | `update` writes the literal dotted key |
| `WriteResult.transform_results` | `firestore_account_repo.py:120-123` | `update` returns `None` |
| Positional `where("f", "==", v)` | `firestore_session_store.py:289,332`; `prompt_v3/firestore_token_repository.py:142,163` | keyword-only `filter=` |
| `ArrayUnion` / `ArrayRemove` | `firestore_whitelist_repo.py:119,143` | the sentinel is stored as the value |
| `SERVER_TIMESTAMP` | 12 sites, e.g. `firestore_whitelist_repo.py:120` | the sentinel is stored as the value |
| `DocumentReference.create()` + `AlreadyExists` | `firestore_dedup_store.py:72`, `firestore_short_link_repository.py:34`, `firestore_memory_fact_draft_repository.py:42`, `firestore_skill_repository.py:236`, `firestore_drive_index_repo.py:633,1496` | no `create` |
| Subcollections (`doc.collection()`) + `list_documents()` | `firestore_skill_repository.py:114` and others | none |
| Auto-id `collection.document()` | `firestore_prompt_repository.py:372` | an id is required |
| `collection_group` | `firestore_drive_index_repo.py:1642` | none |
| `!=`, which also excludes documents missing the field | `firestore_task_search_index.py:143` | unsupported operator |
| `stream(transaction=)`, `get_all(..., transaction=)` | `firestore_user_repo.py:164`; `firestore_skill_repository.py:154,167` | no `transaction` argument |
| Transaction semantics (reads before writes, writes applied at commit, a retry re-runs the body), with **one** global patch of `google.cloud.firestore.async_transactional` | 16 `async_transactional` sites in `src/adapters`; `firestore_ephemeral_store.py:49-89` relies on the retry re-reading; a retry is triggered by an aborted commit (a concurrent transaction), never by an outside write during the body — real Firestore and the emulator block it (P3) | pass-through; the patch targets the drive module only |
| Assert `COSINE` in `find_nearest` | all vector queries use cosine today | cosine assumed |

Two rule changes:

- **Remove the equality-only vector pre-filter refusal** (`firestore_drive_eval_fakes.py:55`) from the generalised fake.
  Firestore accepts range and `!=` pre-filters; production uses them (`firestore_indexed_email_repo.py`,
  `firestore_task_search_index.py:141-152`).
- **Refuse undeclared composite queries.** A query with equality + range/`in`/`!=` + `order_by`, or a vector query with
  pre-filters, must match an index in the rendered `config/firestore.indexes.json` (§4 render step), else the fake
  raises `FailedPrecondition`. This generalises the drive provisioning tests to every collection, so a missing index
  fails in `make check`, not in production. It is why `order_by` in §1.4 items 2–3 was commented out.

**Seeding by raw dict.** Kits seed documents as raw dicts, including legacy shapes (a missing field, a string date, an
unknown enum value), and assert that every reader handles or excludes them on purpose.

### H2. Port contract kits

**Step 0, semantics inventory.** Port docstrings say nothing about the dimensions where the defects live
(`ConsolidationQueuePort.get_pending_batches`, `src/ports/consolidation_queue.py:16-17`, states no order). Before any
kit, for each port: write its contract on seven axes into the docstring — **order, limit, legacy documents, tenancy,
atomicity, idempotency, errors** — derived from what the callers assume (grep the call sites). An Opus reviewer
approves it. The kit then tests the docstring.

**The kit.** One abstract test class per port, run against every implementation: the Firestore adapter on H1 in
`make check`, the same adapter on the test database in `make test-cloud` (§4), and any in-memory implementation. The
drive parity suite is the precedent.

**Inventory meta-test.** Every class in `src/adapters/` that implements a stateful port (all `firestore_*`, `gcs_*`,
`fact_management_adapter`, both queues, `prompt_v3/firestore_*`) is instantiated in at least one kit on H1, or has an
allowlist entry with a reason. The allowlist may only shrink.

### H3. Network guard

`pytest-socket` (pinned), enabled by an autouse fixture in a new `tests/unit/conftest.py` (calling
`pytest_socket.socket_allow_hosts`) and in the `test-g2-offline` target, not by `pytest.ini` addopts (that would also
hit integration and cloud):

- **`--allow-hosts=127.0.0.1,::1` and `--allow-unix-socket`.** Socket creation and binding stay allowed; only
  `connect()` to a non-loopback address raises. Without the unix-socket allowance every asyncio event loop fails
  (`socketpair` in the selector loop). Loopback tests stay allowed: the sandbox supervisor's two-process test
  (`tests/unit/web/test_sandbox_supervisor.py:277,303,382`) is `slow`, so it runs in `make ci`; the TLS media server
  (`tests/unit/adapters/drive_media_fixtures.py`).
- **gRPC tripwire** in the same conftest: `google.auth.default` raises, and `grpc.secure_channel` /
  `grpc.aio.secure_channel` raise unless the target is localhost. Firestore talks through gRPC's own sockets, which
  `pytest-socket` does not see; unit tests do build real clients with anonymous credentials.
- **Out of scope, stated:** subprocess children (drive parser, sandbox engine, `ffmpeg`).

### H4. Prompt-snapshot vocabulary tables

Tokens are prose, so "every enum value a token names" cannot be extracted generically (`HISTORICAL` is a legitimate
`context_priority` value next to the `ARCHIVED` state; Tasks prose names `task_id`/`list_id` as output fields). H4 is
**per-agent declared vocabulary tables** — `(token, field) → enum class or tool schema` — checked against the code
over `prompts_snapshot/`, starting with Consolidation and Tasks. Each table also checks that every token a profile
lists exists and every token a blueprint orders is assigned.

Limits, stated: the snapshot is only as current as the last `firestore_utils/snapshot_pull.py`; user and account
overrides are never mirrored (`prompts_snapshot/README.md:24`), so the USER and ACCOUNT levels are invisible to H4
(P10 tests the resolution itself).

### H5. Field registry (facts only, for now)

Grep cannot find readers (`FactEntity(**data)` reads every field; `state` has over a thousand whole-word hits in
`src/`). H5 is **AST-based**: attribute access `<entity>.field` and literal keys `data["field"]` / `.get("field")` in
modules that import the entity; `**data` construction counts as no reader. Or derive it from the round-trip suite (a
field no assertion observes after a round trip). Facts only; other entities get the H2 round-trip invariant ("every
written field is returned by every reader") instead.

### H6. Spec-status checker

A test over `docs/10_rfcs/`: the status in the RFC index matches the RFC header, and an RFC whose status means
implemented has every path it cites in code blocks present in the tree. Headers are free-form today (`IMPLEMENTED`,
`✅ IMPLEMENTED`, `Phases 1–7 Complete`, `Step 1 live`, some with none), so it ships with a **status normalisation
table** and an **exemption list** (older RFCs citing since-renamed paths). Draft and Proposed RFCs are exempt.

## 4. The cloud test gate (real writes, live data untouched)

**Isolation and guards:**

1. **A separate Firestore database** in the same project, never the live one and never `(default)` (scripts already
   hit it by mistake, §1.3.4). Its name and the GCS test bucket's come from `.env` (the test database name from `.env`;
   secrets rule). The guard enforces a pattern: the name ends in `-test` and differs from the live
   `FIRESTORE_DATABASE`.
2. **`APP_ENV=test`**, which maps to the `test_` collection prefix (`environment.py:52-59`), so even a misrouted client
   cannot land in `development_*` collections.
3. **A dedicated test service account** with `roles/datastore.user` and `roles/datastore.indexAdmin` under an IAM condition on the test database only
   (`setup_test_gate.sh`, 2026-10-21).
   Client libraries and REST enforce database-level conditions; the Console does not.
   - **Local runs impersonate it** (`gcloud auth application-default login --impersonate-service-account=…`); the
     fixture asserts `credentials.service_account_email` equals the test SA before any write. Without this, local runs
     use the owner's project-wide rights and the condition protects nothing.
   - **CI uses Workload Identity Federation**, limited to `refs/heads/main` of this repository; pool, provider and SA
     names live in GitHub repository secrets (`TEST_WIF_PROVIDER`, `TEST_SA_EMAIL`, `TEST_FIRESTORE_DATABASE`, `GOOGLE_CLOUD_PROJECT`). CI has no GCP auth today (`.github/workflows/ci.yml:33`).
4. **Guards check the constructed client** (its database and project), not env vars.
5. **Cloud fixtures build repositories directly from an `EnvironmentConfig`**, never through composition or
   `load_settings()`. Under `APP_ENV=test`, `is_development` is False, so queue names switch to the `-prod` suffix
   (`src/composition/drive_indexing_setup.py:42`, `src/composition/slack_adapter_factory.py:147`) and
   `load_settings()` chases every empty key into Secret Manager.

**One runner at a time.** Global documents (the whitelist doc, system prompt tokens, short-link and invite namespaces,
dedup keys) and cross-tenant scans (`get_stuck_batch_user_ids`, `list_sync_user_ids`) mean per-run tenant ids do not
isolate concurrent runs. A **lease document** in the test database, taken in a transaction with a TTL, serialises CI
and local runs alike. Per-run tenant ids stay for hygiene.

**Cleanup.** Each test deletes what its run wrote; tests of global documents restore the prior content; the wipe of
the whole test database runs only under the lease. Never delete-and-recreate the database (it drops indexes, and a
deleted id is reusable only after minutes).

**Indexes.**

- A **render step** turns `config/firestore.indexes.json` (120 entries: 96 `development_*`, 24 unprefixed legacy or
  dead names) into the test set: keep `development_*`, rewrite to `test_`, drop the rest.
- The index scripts take the database **explicitly**; no live default (today `create_firestore_indexes.sh:9` (resolved 2026-10-22, S1b: the script is retired),
  `create_drive_vector_indexes.sh:13` and `deploy_firestore_indexes.py:33,97` default to the live database).
- `create_prod_vector_indexes.py` is dropped from the list (§1.4 item 12); the missing task-search vector index
  definitions are added to git (§1.4 item 11).
- **Source of truth:** `gcloud firestore indexes composite list` on the live database, diffed against git; a
  difference is a finding.
- Index creation is **not an owner step** since the test service account also holds `datastore.indexAdmin` on the test database (2026-10-21):
  the P5 fixture creates the test-database indexes through the Admin API (the live database's indexes stay an owner step).

**GCS.** A test bucket with object versioning (workspace `history`/`restore` need it), a 1-day lifecycle delete and a
noncurrent-version rule, same guards. **Cloud Tasks:** a paused test queue, so enqueue is real and nothing is
dispatched.

**What runs there:** the H2 kits against the real adapters (this is what proves H1 right), vector search and index
behaviour the fake cannot prove, and **deterministic precondition tests** (`create()` on an existing document,
concurrent transactions (the conflict is an aborted commit; P3), GCS `if_generation_match`) instead of contention
tests, which are slow and nondeterministic.

**Gate:** `make test-cloud`, serial. CI: a separate job after a merge to `main` (like `timing`), never on a PR, never
in `make check`. Cost: named databases have no free quota; reads and writes stay in the cents per run.

**Before any of this, a 1-hour emulator spike** (P3): does the Firestore emulator (`EnvironmentConfig.use_emulator`,
`main.py:108-112`) run our queries, including vector search? If it covers the non-vector part, the parity slice (P5)
runs the non-vector probes there, offline; vector probes still need the test database.

**Result (P3, 2026-10-22).** Emulator v1.22.0 with `google-cloud-firestore` 2.34.1 ran all 30 probes: every H1 write and
query shape and every P5 shape, **vector search included** (COSINE order and distances, `distance_threshold`, equality,
range, `!=` and `in` pre-filters, projection with `distance_result_field`). Two differences, both index refusals: it does
not refuse a composite query or a kNN query without an index. Transactions lock pessimistically, so a conflicting write
cannot be placed inside the body (the outside write waits 2 s, then `Aborted`); the conflict case is concurrent
transactions. So P5 runs offline on the emulator in full, and the test database is needed only for the index refusals,
three points the docs do not pin (null under `!=`, the error class for a precondition on a deleted document, the lock wait)
and one confirming run. Probe: owner's ops journal.

## 4a. Third-party contracts (owner, 2026-10-21)

**Today.** Every third-party adapter is tested by mocking at its boundary, at three depths:

| Depth | Who | What it proves |
|---|---|---|
| SDK method mocked, request kwargs asserted, named rules (`tests/contracts/adapter_contracts.py`, `ADAPTER_WIRE_TESTING.md`) | Claude, Gemini, OpenAI, Grok | our translation matches a rule we wrote |
| HTTP session mocked, responses **recorded from the real API** (`tests/fixtures/onedrive_delta/*.json`) | OneDrive | our parsing matches what the provider really sent once |
| SDK/HTTP client mocked with **hand-written** payloads | Gmail, Slack, Telegram, To Do, Twilio, GCS, Cloud Tasks, Cloudflare SFU, Unsplash, MCP Maps, the realtime voice providers | our code matches what the test author believes the provider does |

A mock is only as true as its author's belief (the recorded misses: OpenAI's `strict` default, a Firestore projection
dropping the distance field, Graph returning raw paths, xAI's running-total audio seconds). The owner's direction:
**contracts in a standard format; mocks must conform to the contracts; the contracts themselves are checked against the
real providers, outside CI.**

### Contracts

- `contracts/providers/<provider>/openapi.yaml`: a **subset in the provider's native OpenAPI version** (3.0 or 3.1) —
  only the operations and fields our adapters use. Each operation carries `x-source` (URL) and `x-retrieved` (date).
- **Recorded fixtures first, authored schemas after.** The misses on record were semantic and were found by recorded
  responses and docs review, not by schema shape.
- Sources, extracted by a script (`scripts/contracts/extract_subset.py`, source pinned by commit or date):

| Provider | Source | Note |
|---|---|---|
| OpenAI | `openai/openai-openapi` (MIT, OAS 3.1) | REST; realtime only as event-schema components |
| Twilio | `twilio/twilio-oai` (MIT, OAS 3.0) | form-encoded PascalCase bodies |
| Cloudflare SFU | the separate Realtime API spec in Cloudflare's docs (CC-BY-4.0: attribution in the repo) | the general `cloudflare/api-schemas` has no SFU data plane (`cloudflare_sfu_adapter.py:10,79-109` calls it) |
| Microsoft Graph | `microsoftgraph/msgraph-metadata` (MIT, OAS 3.0) | needs an **alias layer**: `/me/drive/…` and colon addressing map to `/drives/{id}/items/…`; parsing takes ~19 s and ~1 GB, fine weekly, never per test |
| Gmail, Gemini, Google OAuth (token, revoke, userinfo; `src/services/google_oauth_service.py:26-29`) | Google discovery documents (Gmail and `oauth2` in `googleapis/discovery-artifact-manager`; Gemini from the live discovery endpoint) | a small discovery→OpenAPI subset converter, budgeted in P17 |
| Telegram Bot API | community spec `PaulSonOfLars/telegram-bot-api-spec` (MIT) | extracted, not hand-authored |
| Anthropic, xAI, Slack Web API | authored from the documentation | Slack's official specs were archived in 2021 |
| Firebase ID-token verification | authored from the documentation (token claims) | |

- **Our own HTTP boundaries get contracts too** (`contracts/internal/`): `/worker` (21 `WorkerHandler` task types plus `telegram_update`), the voice control
  plane (`/voice/session-config`, `/voice/delegate`, `/voice/submit-transcript`), the sandbox render API, the indexer
  `/worker`. The server's route tests and the client's tests validate against the same file.
- **WebSocket protocols are not OpenAPI operations.** OpenAI Realtime, xAI Realtime, Twilio Media Streams and the SFU
  legs (`openai_realtime_adapter.py`, `xai_realtime_adapter.py`, `src/handlers/media_stream_handler.py`,
  `src/handlers/sfu_stream_handler.py`) get a **JSON Schema per event type** (OpenAI's realtime components extracted;
  xAI, Twilio and the SFU legs authored) plus **recorded session transcripts**, checked with one helper
  `assert_message(provider, event_type, payload)`. No AsyncAPI tooling.
- Semantic rules a schema cannot express (a default we must override, "never send `temperature`", "this field is a
  running total") stay as named `ContractRule`s in `tests/contracts/adapter_contracts.py`, each pointing at the
  operation or event it refines.

### Mocks conform to the contracts (consumer side, in `make check`)

**The existing SDK-level wire tests stay untouched.** They mock the SDK method and assert its kwargs; SDK kwargs are
not the HTTP body (Gemini restructures `config` into camelCase REST fields; Twilio's `to=`/`from_=` become form fields
`To`/`From`; Stainless SDKs move `betas` into a header). Routing them through an HTTP contract would edit about 500
existing tests under the reviewer rule, and `ADAPTER_WIRE_TESTING.md:134` forbids HTTP mocking as written.

Instead, **new conformance tests at the HTTP transport run the real SDK**:

| Transport seam | Adapters |
|---|---|
| `respx` / `httpx.MockTransport` | OpenAI, Anthropic, xAI (OpenAI SDK), google-genai, python-telegram-bot |
| `aioresponses` | Gmail (raw `aiohttp`, `gmail_provider_adapter.py:26`), Slack `AsyncWebClient`, Google OAuth (`aiohttp`) |
| a custom `twilio.http.HttpClient` | Twilio |

The captured request is validated against the contract operation; recorded response JSON goes through the real SDK
parser. **Cheap first step for the Stainless SDKs:** validate the SDK kwargs against the SDK's generated param types
(`pydantic.TypeAdapter` on e.g. `openai.types.responses.ResponseCreateParams`), which come from the provider's spec, so
an invented field fails and an SDK pin bump carries spec changes.

`ADAPTER_WIRE_TESTING.md` is amended: **SDK-level tests prove translation; transport-level tests prove conformance.**

- One helper, `tests/contracts/openapi_contract.py`: `assert_request(provider, operation_id, method, path, query, body)`
  and `assert_response(provider, operation_id, status, body)`, on `openapi-core` (validates 3.0 and 3.1) or
  per-operation JSON Schemas compiled once and validated with `jsonschema`, cached per process (milliseconds per call).
  `openapi-spec-validator` checks the subset files in a meta-test.
- Response fixtures live under `tests/fixtures/providers/<provider>/` with `_meta {operation_id, source: recorded |
  documented-error, recorded_at}`. A meta-test validates every fixture against its operation and fails on a
  `documented-error` fixture without a doc link.
- A coverage meta-test: every operation in a provider contract has at least one conformance test, and every adapter
  call site maps to an operation (a per-adapter `OPERATIONS` table the test checks against the code).

### Contracts checked against reality (provider side, never in PR CI)

Three mechanisms, cheapest first:

1. **Spec drift, scheduled, automatic.** A weekly GitHub workflow re-fetches the official specs (public, no secrets),
   re-extracts our subsets and diffs them against `contracts/`. A diff posts to Slack and opens an issue. Covers the
   providers with a published spec.
2. **Contract review with Claude, in a session** (owner's preferred). A skill `provider-contract-review`, run monthly
   by a scheduled routine and on demand after an incident or a provider announcement: per provider, read the current
   reference, changelog and deprecation notices; compare with `contracts/`, the `ContractRule`s and the adapter code;
   report each difference with the doc link and a proposed contract change. The owner approves; the change lands as a
   PR, and the conforming tests then show which adapters it breaks.
3. **Live probes, manual or scheduled, never per build** (`make contract-probe PROVIDER=…`): call the real provider,
   validate the response against the contract, and refresh the recorded fixture. Reads where free; writes only where a
   test environment exists or the write cleans itself up:

| Provider | Test environment | Probe |
|---|---|---|
| Slack | **developer sandbox** (free Enterprise org via the Developer Program; needs a card on file) | reads + writes in the sandbox |
| Telegram | **official test environment** (separate test-DC bot, `/test/` endpoints) | reads + writes there |
| Twilio | **test credentials + magic numbers** (validated as live, no calls placed, no charge) | Calls/Messages creation with magic numbers |
| Microsoft Graph (OneDrive, To Do) | M365 developer sandbox is **eligibility-restricted** | owner's account: App Folder `__contract_probe__/` create → read → delete; To Do: a dedicated test list |
| Gmail, Google OAuth | none | reads only, on the owner's account or a dedicated test Google account |
| Our GCP (Firestore, GCS, Cloud Tasks) | the cloud gate's test resources (§4) | full, there |
| Cloudflare SFU | none | session create → close, only if free; otherwise contract review only |
| LLM providers | none; **no generation calls** (§2.1) | spec drift + contract review; free metadata reads (`models.list` / `retrieve`, owner decision 6); an OpenAI Realtime session-create only if it generates nothing |
| Unsplash, Google Maps MCP | Unsplash demo mode; Maps none | reads within free quota |

Probe credentials stay in `.env` / Secret Manager (never in git); probes print counts and diffs, never personal content.

## 4b. Test-time budget: 3 minutes locally; CI may take longer (owner, 2026-10-21)

The budget is for **local runs**: the agent runs the suite hundreds of times a session. CI runs once per push and may
take longer.

**Today.** `make test-unit` takes ~1:34 on the 12-core Mac when the machine is quiet. On 2026-10-21 the same suite with
coverage took 13:24 while other sessions ran their suites (load average ~270): the cost is contention, not the suite.
CI: 4:37 wall (install 0:34, apt `ffmpeg` 0:30, pytest 3:01 on a 4-core runner).

**Local, three tiers:**

1. **Inner loop: `make test-affected`** = `pytest --testmon`. testmon
   selects by executed Python lines, so it misses tests whose input is a data file (H4 over `prompts_snapshot/`, H6 over
   `docs/10_rfcs/`, the index-provisioning tests over `config/firestore.indexes.json`, `tests/unit/test_cloudbuild_*`,
   skill files, fixtures) and code run in subprocesses (drive parser child, sandbox engine); the mandatory
   `make check` before a commit catches those (see the P1 note below). `.testmondata` is seeded into each new worktree from the main checkout, so the first run is not a
   cold full suite. testmon is pinned and never combined with coverage (it pauses the outer measurement).
2. **Before a commit: `make check`, ≤ 3:00 on a quiet machine.** Lint, mypy, the unit suite and the fast contract and
   fake-based suites this RFC adds. `--durations=25` prints the slowest tests as an **advisory report**. There is no
   per-test duration budget (see the P1 note below).
3. **Slow suites only in CI.** `@pytest.mark.slow` mirrors the `timing` pattern (the skip hook in `tests/conftest.py`,
   the Makefile's `-m "not timing"`). The coverage gate is a **run mode** of `make ci`, not a marked test.

**One full suite at a time on the machine.** A pytest hook in `tests/conftest.py` takes `fcntl.flock` on a lock file in
the temp dir, in `pytest_configure` of the **controller only** (not in xdist workers), and only for large sessions
(`-n` given, or a collection above a threshold checked at collection finish). It prints the holder and waits with a
timeout. `fcntl` exists on macOS and Linux (the `flock` command does not exist on macOS), the kernel releases the lock
when the process dies, and the hook covers make, direct `pytest` calls and subagents alike.

**CI.** The PR job switches from `make check` to **`make ci`** (= `make check` + slow tests + the coverage gate),
≤ ~10 min; the repo is public, so GitHub-hosted runners cost nothing and sharding is available. After a merge to
`main`: the cloud gate, timing tests, the skip-ledger job (P22). Weekly: spec drift, mutation metric. Monthly: contract
review. Manual: probes. Docs-only PRs skip `make ci` (F3, `scripts/ci/docs_only.py`); the weekly cloud jobs are the
`Mutation survivors` and `Firestore parity` workflows (artifacts, no Slack; see "Rules revised").

**Built (P1, 2026-10-22).** `make test-affected` (one `pytest --testmon` run, `.testmondata` seeded into a new worktree),
`make ci`, the `slow` marker and the `fcntl` suite lock (`tests/plugins/suite_lock.py`, `tests/conftest.py`, `Makefile`,
`ci.yml`). Deviations from the text above:
- **No per-test duration budget** (owner decision 2026-10-22): the goal, a fast local `make check`, is met by the `slow`
  marker, and a wall-clock limit flakes on a shared runner. `--durations=25` stays as an advisory report.
- **No `datafile` marker, no always-run set, no data-file audit** (owner decision 2026-10-22): testmon misses are caught by
  the mandatory `make check` before a commit, so the marker set and the audit that kept it honest were not worth carrying.
- **testmon needs `--testmon-forceselect`.** testmon 2.2.0 switches selection off whenever `-m` is given ("selection
  automatically deactivated because -m was used"), so `make test-affected` passes `--testmon --testmon-forceselect
  -m "not timing"`. A no-change rerun then selects 0 tests (testmon turns pytest's exit 5 into 0). A new worktree seeded
  from the main checkout's `.testmondata` at the same commit also selects 0.
- **`make check` no longer runs the `slow` tests**, among them 24 of the 36 layering tests of
  `test_req_arch_01_hexagonal_isolation.py` (the 23 over 0.5 s in the calibration plus `test_agents_layer_isolation`).
  Each walks the whole of `src/`: 0.5-3.2 s under coverage, 0.8-2.1 s without, and it moves with machine load. A shared
  parse buys only ~15% (the cost is walking ~600k AST nodes per test), so the decision is to **keep them slow**. They
  run in `make ci`, so on every PR, but not in `make check` or `make test-affected`.
- **The slow threshold was calibrated at 0.5 s under coverage**; a test using a module-scoped fixture is marked in every
  user of that fixture, because under xdist whichever test reaches it first on a worker pays
  (`test_drive_index_hostile_tree.py`, `TestScale`). Tests that pay a lazy heavy import first (markitdown) are marked too.
- `.testmondata` is seeded with SQLite's backup API (`scripts/seed_testmondata.py`), not `cp`: testmon uses WAL.
- Extra knobs, for tests of the machinery only: `PYTEST_SUITE_LOCK_FILE`, `PYTEST_LOCK_REPORT_S`, `PYTEST_LOCK_POLL_S`.

## 5. Work packages

### Semantic mutants (a done criterion until 2026-10-27, now the weekly run only)

A raise-mutant is killed by any test that merely calls the method; the defects found were semantic (ASC for DESC, a
dropped projection field, a missing state flag, a missing tenant check). The **semantic mutation check** is therefore run by a small pytest plugin (generalised from the blind-spot report's prototype), with this operator
set:

- drop or flip an `order_by`;
- remove one `where` (the tenancy filter is mandatory);
- drop one field from a `select` or a `model_dump`;
- replace an `except` body with "return success";
- skip one write in a multi-write path;
- drop one forwarded keyword argument.

Until 2026-10-27 a package was done when every mutant in its set was killed or ruled equivalent by the reviewer. Since the owner's
decision of that date no package runs this check locally: it registers its modules in `TARGETS`, and the weekly `Mutation survivors`
run is the only mutation pass (see "Rules revised"). The reviewer still owns the `*_equivalent_mutants.txt` lines. `mutmut` (P23) is a
metric only, never a done-gate.

### Packages

Effort is realistic working days, after both reviews. Gate: `check` = `make check`; `ci` = `make ci` on a PR;
`cloud` = `make test-cloud` after a merge; `sched` = scheduled or manual.

| # | Package | Gate | Effort | What it catches | Tranche |
|---|---|---|---|---|---|
| P1 | **Test-time budget** (§4b): `make test-affected` (testmon) with worktree seeding; the `fcntl` lock hook; `slow` marker; `make ci` and the PR job switched to it; advisory durations report | check / ci | 1–1.5 | a local loop that grows past 3 min; five parallel full suites | 1 |
| P2 | **Rules and gates**: H3 network guard + gRPC tripwire; `tests/CLAUDE.md` rewritten (fakes with semantics over call assertions; adapters need behind-port tests; no real LLM; MagicMock coverage does not count); coverage per layer with `branch = True`, floor at the re-measured values, only rising; root `*_main.py` into `[run] source`; subprocess coverage configured (`[run] patch = subprocess`) or omitted with a reason; a static "no test imports module X" check with an allowlist that only shrinks; the semantic-mutation plugin | check / ci | 1.5–2 | the convention itself; unmeasured entry points | 1 |
| P3 | **Emulator spike** (§4): which of our queries, vector included, the emulator runs | — | 1 h | decides how much of P5 runs offline | 1 |
| P4 | **H1 fake** (the full add-list, index refusal, raw-dict seeding, one transaction patch) **+ the fact store kit** (S1's round-trip suite folded in; semantics inventory for the fact ports) | check | 2.5–3.5 | the fact defect class; missing indexes offline | 1 |
| P5 | **Fake-vs-real parity slice** (before any other kit is built on H1): mixed-type ordering; missing-field filters and orderings; `!=`/`in`/range + kNN pre-filters; dotted paths; `Increment`/`ArrayUnion`/`SERVER_TIMESTAMP` results; projection + `distance_result_field`; `create()` on an existing document — run on H1 and on the test database (P3 result: vector search runs on the emulator too; the test database is needed for index refusals and the three undocumented points) | check + cloud | 1 + owner infra step | a wrong fake rule spreading into every later kit | 1 |
| P6 | **Access-control stores** on H1 + kits: `firestore_whitelist_repo`, `firestore_mcp_client_repository`, `firestore_oauth_credentials_adapter`, `firestore_iam_adapter`, `firestore_invite_code_repo`; negatives: a revoked email is refused, a token of another client is refused, an expired code is refused | check | 2 | access holes in the stores | 1 |
| P7 | **Inbound gates**: every refusal site in an auth path executed by a test — the deep-research webhook signature (`src/web/deep_research_webhooks.py:102-141`), the Slack route's signature enforcement and dedup (`src/adapters/slack/http_adapter.py:138-140`), MCP OAuth refusals (`src/services/mcp_authorization_service.py:166-167,193-194,400-401`), the sign-in ID-token failure (`src/services/authentication_service.py:441-446`), the `/worker` OIDC gate and Telegram intercept (`main.py:1234-1262`), the Microsoft Graph webhook; a meta-test over each Quart app's `url_map` and the relay, indexer and sandbox routes requiring at least one negative test per route (unauthenticated, bad signature, foreign tenant); a line-level check that every `raise`/4xx return in the auth services and verifiers runs | check | 1–1.5 | an inverted or bypassed gate | 1 |
| P8 | **Prompt vocabularies** (H4) for Consolidation and Tasks | check | 2–3 | `HISTORICAL`, `metadata`, Tasks `task_ref` drift | 1 |
| P9 | **Wiring and reachability**: every `task_type` in the scheduler scripts and every enqueue call site dispatches through `WorkerHandler.handle` to a non-501 handler under a production-shaped container built with fakes; every handler is reachable or allowlisted as manual-only with a reason; every Optional handler dependency is non-None in the production wiring; an unknown `task_type` is refused | check | 1 | dead features, missing triggers, §1.4 items 8–10 | 1 |
| P10 | **Prompt stores**: `firestore_prompt_repository`, `prompt_v3/*` on H1 + kits; 4-level resolution (USER > ACCOUNT > AGENT > SYSTEM) semantics | check | 1.5–2 | a wrong prompt for every agent | 2 |
| P11 | **Every other stateful store, by inventory** (H2 meta-test), ordered by risk: voice tickets / ephemeral store, dedup, notes delivery stamp, quota, long turns first; then indexed email, session store, consolidation and companion queues, user, account, task search index, channel binding, skills, memory-fact drafts, companion memory, email job and exclusions, GCS stores, `gcp_task_queue` | check | 5–7 | ordering, projections, missing fields, tenancy, atomicity in stores that look covered | 2 |
| P12 | **Cloud gate, full** (§4): lease, render step, explicit-database scripts, missing index definitions, GCS bucket, paused queue, WIF for the post-merge job; every H2 kit against the real adapters; precondition tests | cloud | 2–3 | fake ≠ Firestore across all kits | 2 (revised 2026-10-27: lease document and render gate dropped; see "Rules revised") |
| P13 | **Service seams**: handler-level tests that run the **real** `UserNotificationService`, `TaskDispatchService`, `AgentCoordinator`, `SearchEnrichmentService` and `PromptBuilder` over H1 and port fakes for each entry path (worker task types, `ConversationHandler`, `AgentWorkerHandler`), asserting the observable outcome (which channel, which history entry); mutation operator: drop one forwarded kwarg | check | 2 | parameter coupling between services (§1.4 item 6) | 2 |
| P14 | **Scripted-LLM end to end** for every agent that writes state through tools (Consolidation, Notes, Tasks, Smart's local tools `propose_memory_facts` / `draft_skill` / `workspace`, FileManagement drive mutations): a scripted tool call that uses every declared field, asserted on the final store state; a generic check that every declared tool field changes stored state or is refused — nothing accepted and dropped. **P9 hand-over:** the two deep-research rows of the boot test's `MATRIX` (`deep_research_failed`, `deep_research_webhook_event`, 500 until the formatter's prompt blueprint is seeded) must become 200 here; the other three belong to P15 (list in the "Built (P9)" note). | check | 1.5 | tools that report success after dropping input (§1.3.5) | 2 |
| P15 | **Redelivery and interleaving**: each `/worker` task type run twice, sequentially and concurrently (`asyncio.gather`), on H1, asserting one user-visible effect, or documented as idempotent by construction; a gather-interleaving test for each shared mutable singleton (agent instances, factory caches, ledgers). **P9 hand-over:** three reasoned non-200 rows of the boot test's `MATRIX` must become 200 here: `setup_microsoft_todo` and `reindex_task_list` (500 until To Do credentials are seeded) and `email_indexing` (404 "job not found" until an indexing job is seeded); the two deep-research rows belong to P14 (list in the "Built (P9)" note). | check | 1.5 | §1.4 items 1, 4, 5; the 3.6× billing class | 2 | (built 2026-10-27; see "Built (P15, part 1)" and "Built (P15, part 2)"; the `MATRIX` rows wait for card H)
| P16 | **Clock suite**: a pinned clock and a table of instants (both 2026 DST transitions in Europe/Berlin, Europe/Helsinki, America/New_York; month end; UTC midnight) through every function that matches an hour or a day boundary (daily review, billing rotation, drive monthly budgets, reminder `due`, Cabinet 24h cap, scratch 7-day TTL) | check | 0.5 | §1.4 item 7 and its class | 2 | (built 2026-10-27; see "Built (P16)")
| P17 | **Provider contracts, consumer side** (§4a): recorded fixtures first; `contracts/` subsets (extracted, aliased, converted, authored; internal APIs); WebSocket event schemas + recorded transcripts; Stainless param-type validation; transport-level conformance tests per provider; the helpers and meta-tests; `ADAPTER_WIRE_TESTING.md` amended; then the low-coverage adapters: `gmail_provider_adapter`, `slack/http_adapter`, `slack/response_channel`, `microsoft/todo_adapter`, `gcp_task_queue`, Telegram, Twilio, Cloudflare SFU, Google OAuth, the four voice files | check | 10–15 | mocks that disagree with the provider | 2 (revised 2026-10-27: only with a regeneration command per recorded fixture; see "Rules revised") |
| P18 | **Reality checks** (§4a): spec-drift workflow, `provider-contract-review` skill + monthly routine, `make contract-probe` for the sandboxed providers | sched | 2–3 + owner sandbox setup | contracts that disagree with reality | 3 |
| P19 | **Web surfaces, the rest**: `oauth_app`, `user_cabinet_app` route-level tests through the Quart test client on H1 stores — connect flows (with Google token mocks), session cap, every Cabinet write route | check | 1.5–2 | data-exposure bugs beyond the gates of P7 | 3 |
| P20 | **Field registry and spec drift**: H5 for facts (AST); H6 with the normalisation table; one Firestore database default (§1.4 item 13) and a lint test requiring `database=` in scripts | check | 1.5–2.5 | write-only fields, spec drift, wrong database | 3 |
| P21 | **Data audit**: a pure checker per entity (invariants from the H2 inventory), unit-tested in `make check`; the owner runs it read-only against live data; output to `scripts/memory/` | check + owner | 1 | data-class defects like B9 | 3 |
| P22 | **Infrastructure in git**: a skip ledger (a meta-test lists every `skipif`; CI reports which ran) and a post-merge job that runs `npm ci` + the real-node DOCX and PDF render and builds `Dockerfile.sandbox`; a TTL list in git (every model writing `expires_at` appears in it) and the runbook generated from it; a Secret Manager fallback suite (`src/config/settings.py` `get_secret` / `load_settings`) | check / post-merge | 1.5 | tests that run in no gate; TTL and secret drift | 3 |
| P23 | **Mutation metric**: `mutmut` on the store modules, weekly or owner-run; survivors are a backlog | sched | 0.5 | suites that verify nothing, as a trend | 3 (revised 2026-10-27: dropped, the weekly semantic run is the metric; see "Rules revised") |
| P24 | **`tests/integration/` split**: `tests/integration/offline/` (mocked, scripted; may join `make check` if fast) and `tests/cloud/` (§4); the two cloud-writing tests move under the guards or go to a reviewer for retirement | check / cloud | 0.5 | live-data pollution by tests | 3 |

**Built (P2, 2026-10-22).** H3 (`tests/unit/conftest.py` + `tests/support/network_guard.py`; the offline G2 run gets the same guard
from `-p tests.plugins.network_guard` in `make test-g2-offline`, so no file under `tests/integration/` changed), the per-layer
floors (`scripts/check_coverage_layers.py`, `.coveragerc` with `branch = True`, the root `*_main.py` and `patch = subprocess`),
the untested-module check (`tests/unit/test_every_src_module_is_imported_by_a_test.py` + `untested_modules_allowlist.txt`), the
semantic-mutation driver (`python -m tests.support.semantic_mutants`) and the rewritten `tests/CLAUDE.md`. Deviations from the text above:
- **A bare `--cov` replaces `--cov=src`.** A `--cov=src` on the command line overrides `[run] source`, so the Makefile passes a bare
  `--cov` and `.coveragerc` lists `src` plus the five root modules. A root module no test imports (`main.py`) is not in the data at
  all; the layer script counts it as 0 % from its source with coverage.py's parser. `[report] include_namespace_packages = True` is
  what makes a never-imported `src/` file show up at 0 % (`src/` and most subdirectories have no `__init__.py`).
- **The per-file table keeps the line metric.** With branch coverage on, `percent_covered` is branch-aware and `smart_response_agent.py`
  (100 % of lines, 4 untaken branches) would fall below its 100.0 entry. `check_coverage_thresholds.py` now reads
  `percent_statements_covered`, so every entry means what it meant when written; raising entries to the branch-aware value is the
  owner's call (the per-layer floors hold the branches).
- **Floors carry a 0.3 pp noise margin** below the measured value (owner decision 2026-10-22; deviates from §7 decision 3 'at today's values').
- **The global `--cov-fail-under=70` stays.** Line coverage under the old config was 90.6 %; branch-aware under the new one 87.9 %
  (never-imported files now count at 0 %). Both are far above 70, so the number is unchanged.
- **The module check is `slow`** (parsing ~1 300 test files takes ~3 s), so it runs in `make ci`, not in `make check`.
- The mutation driver's operators are as listed in §5 above; `except-success` returns `True`/`[]`/`{}`/`""`/`0`/`None` by the return
  annotation (rule in `tests/support/semantic_mutants/operators.py`). First run, `firestore_repo.py` against
  `test_fact_store_roundtrip.py`: the survivors sit on lines the round-trip file never executes (queries, projections,
  observation writes): P4's backlog.
- **`doc-drop` is the dict-literal case of "drop one field"** (a key of a dict written to a store).
- **coverage.py cannot tell MagicMock coverage apart:** the floor counts it; a reviewer does not accept it as the adapter's test (§2.7).

**Built (P4, 2026-10-22).** H1 (`tests/support/firestore_eval.py`) and the fact store kit. Deviations from the text above:
- **H1 is standalone.** It imports nothing from the drive fake. The drive suites keep `tests/unit/adapters/firestore_drive_eval_fakes.py`; moving them onto H1 is not done.
- **The index refusal is opt-in per `EvalDb`** (`indexes=` a path or parsed list, `groups=` fake collection name to declared group), because existing users name their collections arbitrarily. The fact kit's `store` fixture turns it on against `config/firestore.indexes.json`.
- **Documented in the module, not built:** no phantom protection in transactions (a query that returned nothing is not protected); single-field index exemptions (`"indexes": []`) in the config are ignored; a transaction used outside the decorator is not refused; `list_documents()` omits "missing" parents.
- **`# P5 verifies`, each marked in `firestore_eval.py`:** a value of another type counts as "not equal" for `!=`; `ArrayUnion`/`ArrayRemove` report a null `transform_result`; `set()` keys are literal field names (only `update()` splits dotted keys); the order of several implicit inequality fields (sorted by field path); equality plus `array_contains(-any)` merges without a composite; a composite is not served in reverse (exact directions).
- **The fact kit** is `tests/unit/adapters/test_fact_{repository,management,write}_kit.py` with helpers in `tests/support/fact_kit.py`. The S1 round-trip file stays in place as the kit's first file: §6's "folded in" means kept, not moved.
- **The semantics inventory** is the docstrings of `src/ports/repository.py`, `fact_management_port.py` and `fact_write_port.py`, seven axes each (Opus-approved); the kit tests them.
- **Mutation check (§5):**
  - `firestore_repo.py`: 100 killed, 12 equivalent (`tests/unit/adapters/fact_store_kit_equivalent_mutants.txt`), 9 in methods with no caller in `src/` (the owner decides: delete the methods or accept the survivors).
  - `fact_management_adapter.py`: 7 of 7 killed. `fact_write_service.py`: 2 of 2.
- **Branch coverage:** `firestore_repo.py` 43 to 78 %, `fact_management_adapter.py` 83 to 97 %.
- **Four known defects are pinned by strict xfails** (they fail the suite when fixed, so the fix removes the marker): the cached biographical context swallows a backend failure; the biographical refresh ignores `facts_limit`/`principles_limit`; a failed merge leaves the sources superseded; `add_facts_batch` fills `saved_ids` only when deduplication is skipped. The fixes are outside this package.

**Built (P5, 2026-10-22).** The fake-vs-real parity slice. Files: `tests/support/firestore_parity.py` (40 scenarios, 34 when P5 was built, four added by P6 and two by P7; one documented expected value
each, the index groups), `tests/unit/support/test_firestore_parity.py` (H1 in `make check`; the emulator when `FIRESTORE_EMULATOR_HOST` is set),
`tests/cloud/` (`conftest.py` guards and index fixture, `test_firestore_parity_cloud.py`), `make test-cloud`. The same scenarios run on H1, the
emulator (35 of 38; it does not refuse a missing index) and the test database. Deviations and results:
- **Results.** The emulator agreed with every expected value. The test database confirmed every expected value of the first 32 scenarios,
  the documentation-silent ones included, except one: the guess that a composite index is scanned in reverse was **refuted** (exactly flipped
  directions give `FailedPrecondition`), so the expected value was corrected, and H1, which refused it, had been right. H1 differed from Firestore
  on two scenarios, both fixed in `firestore_eval.py`: the type order (NaN before numbers; bytes < reference < geo point < array < vector < map)
  and a `last_update_time` precondition on a missing document (`FailedPrecondition`, before the existence check; a plain `update` is still
  `NotFound`). P3's open points are settled: `!=` excludes a field set to null, and a precondition on a deleted document is `FailedPrecondition`.
  Every `# P5 verifies` comment now carries its verdict. Review round 1 added two scenarios (filters on two range fields added in reverse order;
  equality plus `array_contains_any`), a reference and a vector in the type order, document ids that run against the value order, and an
  observable distance-field check; H1's index requirement now sorts the range fields as its implicit ordering does. All 34 scenarios were
  confirmed on the test database (2026-10-22, 34 of 34 in 59 s), the vector `order_by` in the type order included; with P6's four, the
  test database ran all 38 (2026-10-22, 38 of 38 in 65 s).
- **Transactions.** The conflict scenario runs two transactions with staggered starts, not three in lock step: the emulator's lock timeouts
  exhausted the SDK's five attempts in 3 of 8 runs (three) and 4 of 15 (two in lock step), and none in 20 staggered runs.
- **Index creation on the test database is a fixture** (Admin API, fixed collection ids `test_parity_idx_*`, created once, READY wait 15 min), not
  the render step of §4; the render step and the lease document come with P12. `make test` ignores `tests/cloud`.
- **Run:** `make test-cloud` (sets `RUN_CLOUD_TESTS=1`; a bare pytest skips `tests/cloud`), by hand, with the owner's (or root's) credentials able to impersonate the test service account; one runner at a time.
  A run that creates indexes may fail scenarios of those collections even after READY (seen once, cause unknown): re-run.

**Built (P6, 2026-10-22).** The access-control stores on H1. Deviations and results:
- **Semantics inventory** = the docstrings of the four access ports with callers (`whitelist_repository`, `mcp_client_repository`,
  `oauth_credentials_port`, `invite_code_repository`). `IAMPort` / `FirestoreIAMAdapter` have no caller in `src/`, so no kit; deleting
  them is an open owner discussion.
- **Kits:** `tests/unit/adapters/test_{whitelist,mcp_client,oauth_credentials,invite_code}_store_kit.py`, the real adapters on H1 with the
  index refusal on. The negatives run through the real callers: the `mcp` SDK's `TokenHandler` -> `MCPAuthorizationService` for the MCP
  refusals, `InviteCodeService` over the real user, account and whitelist repositories for the invite ones.
- **Results:** 140 passed and 7 strict xfails, each a known defect: L1 whitelist case, L2 invite code collision, H1/H2 MCP double exchange
  (auth code, refresh token), H3 invite double consume, H4 OAuth grant recreated after a disconnect, invite roles written uppercase. The
  owner chose to fix whitelist case, role case and H4 in a follow-up PR and to leave the races pinned.
- **Mutation:** invite 3/3, mcp 6/7, oauth 3/4, whitelist 0/12 killed. The 14 survivors are all in methods with no caller (the reviewer
  accepted them as dead; deleting those methods is an owner discussion).
- **Branch coverage:** whitelist 0 -> 71 %, mcp 0 -> 99 %, oauth 38 -> 88 %, invite 85 -> 100 %.
- **Parity:** four scenarios added for the H1 behaviours the kits rely on (see the P5 note: 38 of 38 on the test database).

**Built (P8, 2026-10-22).** H4 for Consolidation and Tasks. Files: `tests/support/prompt_vocabulary.py` (readers: profile, blueprint, tokens,
`block_keys`, `field_line_words`, `pattern_words`, `returns_keys`, `groovy_tools`), `tests/unit/prompts/test_consolidation_vocabulary.py`, `tests/unit/prompts/test_tasks_vocabulary.py`.
Each file opens with its declared table. Consolidation: `(token, field, enum, mode, allowed_other)` rows against `FactDomain`/`TemporalClass`/`FactState`/`ContextPriority`
(the taxonomy blocks must equal the enum's members exactly; every other token's lines naming a field may carry only members plus the row's contrast words; the closed lists — the four Taxonomy_Enforcement constraint lines and the `create_fact` `temporal_class`/`context_priority` descriptions — are `exact:` rows, equal to the enum's members, so a dropped member fails too), the
`@tool` blocks of `CONSOLIDATION_TOOLS` against `ConsolidationAgent._get_tool_declarations()` (names, parameters, nested `fact_attributes`/`updates` keys, code `required`
present in the prompt; `update_fact.metadata`, already in the schema, is guarded here), the keys `search_existing_facts` says it `returns:` against a real `FactManagementAdapter.search_existing_facts` result (it said `temporal`; the key is `temporal_class`, fixed), and every declared tool named by a token (`count_words` is declared in code and named by
no rendered token: listed as the one exception). Tasks: tools the tokens name are declared and the reverse, every required argument is named, every `*_id`/`*_ref` identifier is
an argument or a result key (the result keys come from a real `_format_task_list` call), every result field `OUTPUT_FORMAT_TASKS` lists is a real result key and `ref` is named where the tokens tell the model what a search returns, every `tool(arg=value)` the prose writes uses a declared tool, argument and enum value, each sub-field a token names in parentheses (`linked_resources (display_name + web_url)`) is a key of that result field's entries, and the listed
status values equal the `update_task` enum. Both files check that every profile token exists and the blueprint's `class_order` equals the profile tokens' classes, one each.
Two drifts were live and are fixed **in the snapshot only**: the `context_priority` value `HISTORICAL` (the enum's is `ARCHIVAL`) in `CONSOLIDATION_TAXONOMY`/`_POLICIES`/`_TOOLS`,
and `task_id`/`list_id` where the Tasks tools take `task_ref` and return `ref` (`COGNITIVE_PROCESS_TASKS`, `OUTPUT_FORMAT_TASKS`), plus `url` -> `web_url` for linked resources and `attachments (filenames)` -> `(filename)`. **The Firestore upload is the owner's step**
(`snapshot_upload.py --apply`); until then production still renders the old text. Not covered: `COGNITIVE_PROCESS_CONSOLIDATION` and the universal profile (production renders
neither; they still say `HISTORICAL`), prose that names a value outside a line naming its field, the `TAXONOMY` keys outside the four definition blocks (e.g. `decision_logic`) and its singular `domain:` lines (`Directive_Maintenance`),
the `returns:` lines of the Consolidation tools other than `search_existing_facts`, a field added to the Tasks output list that the result does not carry, USER/ACCOUNT overrides.

**Built (P7, 2026-10-22).** Inbound gates. Deviations and results:
- **Inventory first:** ≈120 routes over the main service, the MCP SDK app, the relay, the indexer and the sandbox, each with its caller,
  its gate and its refusal test (or none), and the refusal sites of every auth-path file with the lines no test ran.
- **The route probe** (`tests/unit/web/test_every_route_refuses_anonymous.py`) builds the 16 blueprint factories with their real gates and
  tripwires behind them, and sends every one of their 99 routes, plus every route of the MCP SDK app, an anonymous request: refused, nothing
  touched or written. A `PUBLIC` list holds the routes public by design, one reason each. An AST companion ties every `register_blueprint` in
  `main.py`, `indexer_main.py` and `sandbox_main.py`, and `_is_mcp_path`'s literals, to what the probe builds.
- **`/worker` is held by an AST check** (`tests/unit/test_main_worker_route_gate.py`: the gate is awaited first, its 401 returned unchanged),
  the owner's choice over moving `/worker` out of `main.py`; tidying `main.py` is an open owner topic.
- **The inventory's negative tests**, route-level on real repositories over H1: deep-research Svix HMAC, Slack signature, Graph webhook, voice
  OIDC, Cabinet owner and foreign tenant, every `/auth/*` refusal, MCP consent and the SDK app, the relay's ticket.
- **Known defects as strict xfails:** the open redirect after sign-in (a fix PR follows); the Graph webhook trusting the URL's user id (owner:
  not fixed); two client errors answered 500 instead of 401/400 (`/auth/refresh` with an access token, `/register` with null `redirect_uris`).
- **F1:** `.coveragerc`'s `\.\.\.` exclude matched a `def` whose signature holds `...` and hid the whole function (1 686 statements in 142
  files). Anchored to a line that is only `...`. Floors re-set to the honest values with the owner's OK (local run; the CI runner's numbers
  re-adjust them): domain 98.1 → 98.0, ports 92.2 → 92.0, services 91.2 → 91.3, adapters 80.6 → 81.8, web 63.1 → 75.2, composition
  89.9 → 90.4, root entry points 34.1 → 33.9; infrastructure, agents, handlers and src (other) unchanged.
- **C1:** the 18 auth files are in the per-file table at 100 % lines; the lines no test ran are closed with behaviour tests.
- **The "Telegram intercept in `main.py`" in the P7 row no longer exists:** `telegram_update` is dispatched by `WorkerHandler`.

**Built (P21, 2026-10-23).** The data-audit framework and the fact checker. Files: `scripts/data_audit/facts.py` (pure: `check_fact(doc_id, doc)` and
`check_lineages(docs)`), `scripts/data_audit/run.py` (the read-only runner; `CHECKERS` maps an entity to its collection property and its checks),
`tests/unit/scripts/test_data_audit_facts.py` and `test_data_audit_run.py` (in `make check`). Invariants, each from the `FactRepository` docstring or
`_LIFECYCLE`, as classes: `id_mismatch`, `missing_account_id` (a legacy `owner_id`-only document), `missing_state` / `invalid_state`,
`is_current_mismatch` / `valid_to_mismatch` (against the state), `missing_created_at`, `reported_date_not_timestamp`, `missing_vector` /
`missing_tags_vector` / `missing_metadata_vector` (absent, empty, or stored as a plain array instead of a `Vector`), `invalid_entity:<field>` (`FactEntity` validation; the fields that have their own class are
not repeated), and across documents `lineage_multiple_current` (at most one `is_current=True` per lineage; a lineage with none is legitimate, because a merge gives the merged fact a new lineage and supersedes its sources in theirs). The owner runs `python -m scripts.data_audit.run facts
[--account <id>] [--limit N]`: it only reads, writes one JSON report (counts and offending document ids, no field values) under `scripts/memory/`, and
prints counts only. S5 adds the expiry invariants; other entities join with their kits (P6, P11).

**Built (P9, 2026-10-23).** Wiring and reachability. Deviations and results:
- **The container is the real application graph.** The "production-shaped container built with fakes" is
  `src/composition/application_graph.py:build_application_graph(config, db_client)` (`ApplicationGraph` plus that function). It builds every
  non-web object `main()` built inline; `main()` keeps the I/O (telemetry, settings, the Firestore client and its warm-up) and the web layer
  (blueprints, voice, MCP, `/worker`, hypercorn) and passes typed `app.<field>`s (no `blueprint_kwargs`). `main.py` went from 1442 to about 635 lines.
- **Declared deviation from the plan: one function.** The builder is one function of about 750 lines, not section helpers (splitting meant passing
  about 40 shared locals; the move had to stay verbatim). Split when the next change needs one part of the graph alone, or at "tidy main.py"
  (`decisions/application_graph_builder.md`).
- **How tests build it.** They build the same graph over the H1 fake and a recording Cloud Tasks SDK client, in a sealed production-shaped
  environment: the `alek-bot-dev` deploy step through the real `load_settings()`, with `K_SERVICE` set (`tests/support/prod_env.py`, `prod_graph.py`).
- **"Every Optional handler dependency non-None" covers the whole graph,** after `start()`: handlers, platform adapters, one user's eager and lazy
  agents. Every `None` is in `wiring_none_ledger.txt` with a reason, and container-owned instances are counted exactly in
  `wiring_instance_ledger.txt` (persistent-store adapters also over the full walk); both ledgers fail both ways.
- **A boot test runs the real `main()` offline** (`tests/unit/test_main_boots_offline.py`): it pins the 102-rule `url_map` and the kwargs of every
  blueprint factory call, and sends every trigger through `/worker` against a pinned per-type table. `tests/unit/test_worker_triggers.py` ties the
  Cloud Scheduler jobs and the enqueue sites to `WorkerHandler`'s branches; no handler is manual-only. An unknown `task_type` answers 400.
  `test_main_construction_allowlist.py` limits what `main.py` constructs itself, and a structural test requires every `BaseAgent` subclass that takes
  an execution context to store it (reasoned allowlist).
- **Mutation.** Two operators, `wire-drop` and `call-skip` (`--operators wiring`), make the mutation check reach wiring. Result (final runs on the PR head,
  2026-10-23, P9 tests plus every unit test that names the module): `application_graph` 350 (346 killed, 4 equivalent), `main` 112 (112 killed),
  `service_container` 168 (167 killed, 1 equivalent), `user_agent_factory` 278 (273 killed, 5 equivalent), `slack_adapter_factory` 42 (42 killed),
  `telegram_adapter_factory` 33 (33 killed): 983 mutants, 973 killed, 10 ruled equivalent by the reviewer
  (`tests/unit/composition/wiring_equivalent_mutants.txt`, each with its reason), 0 survived, 0 error. About 5 h of runs on 4 vCPU.
- **Coverage floors raised** to min(VM, CI runner) − 0.3: composition 90.4 → 94.1, root entry points 33.9 → 76.0 (the graph tests and the boot
  test run `main()` and the builder), handlers 91.6 → 92.7, adapters 82.8 → 83.5, src (other) 70.1 → 74.4.
- **The tests that build the graph or boot `main()` are `slow`,** so they run in `make ci` (or with `RUN_SLOW_TESTS=1`), not in `make check`;
  the source-reading guards and the checker self-tests are not (`tests/CLAUDE.md` -> "The production graph").
- **The owner's check after the first deploy:** the rollout notes (ops). The rollback command and the `:latest` rule: `docs/07_deployment/README.md` -> "Standing levers".
- **Declared behaviour deltas of the move** (all harmless; the T2 snapshot diff shows nothing else): a malformed Telegram secret now exits 1 with a
  traceback (was exit 0, nothing listening either way); construction order (the coordinator after the container, Telegram before voice, queue
  creation and the loops after the whole build) changes the order of the startup lines; `exc_info` on infrastructure errors; the blueprint factories
  keep their `try` blocks; imports of `google.cloud.logging`, Playwright, Anthropic, Grok, the OpenAI transcription adapter and the Telegram and
  Cloudflare modules happen at `import main`; Telegram is all or nothing (an exception after `create_adapter` leaves `telegram_adapter = None`
  and nothing registered). Three more: a failure while building the Slack adapter, HTML renderer or audio adapter, or in `register_handlers()`,
  logs "Infrastructure Initialization Error" with a traceback instead of "Runtime Error" (exit code 1 in both); when `get_blueprint()` fails,
  `PlatformAdapterFactory.register("telegram", …)` has already run (that registry has no reader, P9-D5); Telegram is built in the dead Socket-mode branch too.
- **Defects found and fixed in this package:**
  - P9-D1: sync `document` delivery was dropped, `doc_delivery_service` was never given to the two `ConversationHandler`s;
  - P9-D8: cross-provider failover was dead for `web_search`, `tasks`, `domain_researcher`, `tutor` and `lelik_summarizer` (the execution context was
    never stored), now wired (real only when the user picks claude, for `tasks`); `consolidation` and `tutor_extractor` get `fallback: None` (owner
    ruling 2026-10-23: a memory write never moves to another model on an outage; no live change today, they never stored a context), and so does
    `lelik_summarizer`, through a new strategy entry with quick's providers (owner ruling 2026-10-23, a conscious trade-off: a lost call note,
    alerted, over a second summarizer). So failover is live for four agents (`tasks` only on claude). Consequence: the five agents now record
    failures on the process-wide per-provider breaker that Smart shares (and a success clears them).
- **Defects found and not fixed:**
  - P9-D2: the email classifier's usage is neither billed nor captured. A roadmap item (the owner's quality roadmap, 14b), an owner decision;
  - P9-D6: `ConversationHandler.security_port` is never passed, and P9-D5: the `PlatformAdapterFactory` "telegram" registration has no reader
    (both on the owner discussion agenda);
  - P9-D3: `open_file` on an audio file answers "transcription unavailable" because `FileConversionService` has no audio service. Not fixed:
    deferred by the owner (2026-10-23) to the External Files RFC's U3 stage. Wired into the shared converter, the transcriber would run on every
    text path (chat, workspace, drive files up to 5 MB, coordinator prefetch), unmetered and uncached, and would bypass the drive transcription cap.
    The ledger line is `defect:D3`; the converter's audio branch is tested with an injected fake (`test_file_conversion_service_audio.py`);
  - P9-D4: `AgentWorkerHandler.task_queue` is annotated `TaskDispatchService` (`agent_worker_handler.py:65`) and gets the raw queue
    (`application_graph.py`); harmless, move as is;
  - P9-D7: a duplicate `/health` rule, the Cabinet's shadows `main()`'s (harmless; pinned in the `url_map`).
- **Defects settled by the owner:** P9-D9, Telegram has no channel binding: Slack-only by design (ruling 2026-10-23; the ledger line is `by-design`).
- **Observability residual of P9-D8:** `llm_fallback` log lines carry no agent type, and `prompt_content` and Logfire record the primary provider
  and request for a call that a fallback served. Billing is right (the model that ran is priced).
- **Residuals, not closed by P9:**
  - a wrong but non-None object of the right type;
  - producer and consumer payload fields that do not match (P15);
  - an enqueue literal in dead code;
  - the rollback env switches (the `*-rollback` and `*-forward` Makefile targets);
  - the wiring of `indexer_main`, `job_main` and `relay_main`;
  - four environment reads made at import time, before the seal sets the deploy's values: `ENABLE_GROUNDING_ATTRIBUTION` and
    `DEEP_RESEARCH_SECOND_PASS` (`agent_config.py`), `DOCS_OWNER_USER_ID` (`user_cabinet_app.py`) and the logger's `K_SERVICE`; no assertion depends on them today;
  - a secret that is empty in Secret Manager (the sealed environment fills every secret name with a shaped fake);
  - the reasoned non-200 trigger rows: `deep_research_failed` and `deep_research_webhook_event` (500, the notice formatter's prompt blueprint is missing
    from the empty database) and `setup_microsoft_todo` and `reindex_task_list` (500, no To Do credentials seeded), and `email_indexing` (404 "job not found", no indexing job seeded). They must become 200 in later packages: **P14 owns the two deep-research
    rows** (the LLM formatter), **P15 owns the other three** (the redelivery payloads: seed the To Do credentials and an indexing job).
- **Left for "tidy main.py":** the voice closures; `/worker`, `/health` and `/favicon` as a blueprint (then `test_main_worker_route_gate.py` retires);
  the MCP dispatcher; the `slack_adapter.start` patch; the dead Socket-mode branch; the duplicate `/health`; the main.py allowlist entries.

**Built (P16, 2026-10-27).** The clock suite. Test-only: no `src/` change. Files and deviations:
- **Harness.** `tests/support/clock_instants.py` (self-test `tests/unit/support/test_clock_instants.py`): the 2026 DST instants, the skipped and
  repeated local hours, fold-0 and fold-1 anchors, month ends, UTC and summer midnight. `pin_datetime(monkeypatch, module, instant)` replaces a
  module's `datetime` with a subclass whose `now(tz=...)` answers the pinned instant (re-pinned per step; `isinstance` kept by a metaclass). No
  freezegun or time-machine: a pin is per module, so a test names the module whose clock it moves.
- **Tests.** Reminders: recurrence across DST (`test_recurrence_instants.py`), firing (`test_reminders_due_instants.py`), NotesAgent and Cabinet
  local-time input. Daily review and Gmail auto-index (`test_autoindex_hour_instants.py`, `test_daily_review_*`), plus a Gmail `after:` wire test
  (`test_gmail_review_query_wire.py`, `tests/support/gmail_wire.py`). Billing rotation and the daily summary. The Cabinet 24 h cap, the workspace
  scratch TTL, month and day keys. To Do times at the Graph boundary (`test_todo_time_instants_wire.py`).
- **Zones.** Europe/Berlin (UTC+1/+2), Europe/Helsinki (UTC+2/+3) and America/New_York: two offsets around the same EU switch
  instant plus the US rules.
- **Defects pinned as strict xfails** (the id is in each reason; the fix removes the marker):
  - P16-D1: the Gmail auto-index hour match ignores DST (a skipped hour never runs, a repeated hour runs twice, an unknown zone aborts the fan-out).
  - P16-D2: the review header carries the UTC date.
  - P16-D4: recurrence drops `fold` (DAILY in the repeated hour returns an instant before the anchor; HOURLY and 15-minute rules lose a real hour on
    the fall-back day).
  - P16-D5: a skipped local wall time is accepted as the earlier offset; an unknown Cabinet zone answers 500.
  - P16-D6: `/auth/refresh` mints a fresh 24 h access token (the cap from login is not enforced).
  - P16-D7: the billing summary's Month line mixes months after traffic past 00:00Z on the 1st.
  - P16-D10: To Do sends an aware time's wall-clock digits labelled UTC (due, start, reminder).
  - P16-D11: the automatic To Do reminder is 20:00 UTC. A fix needs a zone input, so its reviewer rewrites that test.
- **D3, restated.** The review's port window is anchored at run time, but the Gmail query is day-granular, so reviews overlap by up to a day and the
  fall-back day leaves a one-hour timestamp hole that `after:` covers. If Gmail reads `after:` in Pacific time, that hole could be a miss for a
  Helsinki user. Unverified; an owner question, not a test.
- **Rejected as non-defects after review:** an ambiguous local time resolving to fold 0 (RFC 5545 says so); the UTC billing day (by design); the
  monthly drive budgets in UTC (documented).
- **Not covered:** the drive's `_search_v2_alerts` and `_sweep_orphans`; the To Do `update_task` path; the owner's choice whether an ambiguous wall
  time should be refused.

**Built (P15, part 1, 2026-10-27).** Redelivery and interleaving. Test-only: no `src/` change. Files and deviations:
- **Route claim.** `tests/unit/test_worker_redelivery_route.py` (on main): the claim through main's `/worker` route; before it, dropping `delivery=`
  left every test green.
- **HTTP boundary.** `tests/support/http_boundary_fake.py` (self-test `tests/unit/support/test_http_boundary_fake.py`): one fake of the aiohttp
  surface the To Do, Gmail and Slack webhook adapters use. `install(monkeypatch)`; exact-path routes (query ignored, `prefix=True` opt-in); an
  unrouted URL fails loudly and is recorded; `raise_for_status` raises with a real `aiohttp.RequestInfo`; `json()` raises `ContentTypeError` on a
  non-JSON body unless `content_type=None`; form bodies are recorded. Presets: an empty To Do account, Gmail, the Slack webhook. Later P15 cards build on it.
- **Payload fields.** `tests/support/worker_payload_fields.py` + `tests/unit/test_worker_payload_fields.py`: every `/worker` producer (enqueue
  sites, Scheduler bodies, the queued Slack event) against every consumer read. Required keys are written by every producer, every read key is
  written by every producer, every written key is read. Allowlists `UNREAD_BY_DESIGN` (queued Slack `enqueued_at`) and `UNWRITTEN_BY_DESIGN`
  (video `provider`: Grok-only by `VIDEO_GENERATION_RFC.md` decision #8; the queued Slack event's absent `task_type`) fail both ways. The producer
  tests are `slow` (they parse all of `src/`).
- **Interleaving tests.** `asyncio.Event` gates; each test asserts its gate was reached, and each defect has a passing control. Factory, delegation
  engine, `$consolidate`: `tests/unit/composition/test_user_agent_factory_interleaving.py`,
  `tests/unit/infrastructure/test_delegation_engine_gather_interleaving.py`, `tests/unit/handlers/test_consolidate_command_interleaving.py`.
  Caches and breaker: `tests/unit/services/test_prompt_cache_interleaving.py`, `tests/unit/services/test_channel_binding_interleaving.py`,
  `tests/unit/agents/test_breaker_stale_success_interleaving.py`.
- **Defects pinned as strict xfails** (the id is in each reason; the fix removes the marker):
  - P15-S1: a lazy agent built while an unlocked eviction runs stays registered and untracked.
  - P15-S1b: an admin cache reset during a build in progress is undone by the build's cache write.
  - P15-S2: a cache write-back after an invalidation brings back the old value (prompt assembly, channel binding).
  - P15-S3: `$consolidate` overwrites messages appended meanwhile.
  - P15-S4: after an eviction the next delegation of the same turn to an eager specialist gets "Unknown recipient".
  - P15-S5: a cancelled child in the delegation gather crashes the turn (tool batch and fan-out).
  - P15-S6: resolved file text is written into the model's own tool-call args.
  - P15-S7: a stale success closes a tripped provider breaker.
- **Known limit.** The nested `context` dict of `agent_execution` is not checked per key: producers forward a runtime dict and the consumer passes
  the whole dict to the agent.
- **Built in part 2** (next note): per-type double runs, retry after a partial effect and the agent instance-state meta-test. Still open: the three
  `MATRIX` rows of the P9 hand-over (card H, after P14's `MATRIX` edit).

**Built (P15, part 2, 2026-10-27).** Redelivery, retry after an effect, and agent instance state. Test-only: no `src/` change. Ten new files:
- **The redelivery world.** `tests/support/redelivery_world.py` (self-test `tests/unit/support/test_redelivery_world.py`): the real `WorkerHandler`,
  claim store, note store, `UserNotificationService`, `RemindersService` and `GcpTaskQueue` on one `EvalDb`, a scripted agent turn and recording
  channels. `deliver_twice` sends two task names the claim does not merge, in sequence or under `asyncio.gather` (`names=None` sends Scheduler bodies);
  `deliver_with_retry` redelivers only when Cloud Tasks would (a raise, `>= 500`, `429`); `Rendezvous` forces a gathered interleaving where the read is
  a query (a query never yields on `EvalDb`; `yield_on_get` covers document reads only) and reports `met` / `stalled`. `require()` and
  `Twice.reraise()` raise `PreconditionFailed`: inside a strict xfail every precondition uses them, because a bare `assert` precondition would turn a
  broken setup into a silent XFAIL. Each xfail was shown to report FAILED with a broken setup.
- **Per-type double runs.** `tests/unit/handlers/test_worker_redelivery_notify.py` (a one-time reminder fire delivered twice posts once and the second
  answers `note_gone`; a recurrent fire posts once through the stored stamp), `..._todo.py` (To Do setup and an orphan renewal delivered twice),
  `..._scheduler.py` (the Scheduler types, below), `..._chains.py` (companion extraction, the Telegram intake `dedup_key`, an owner's click on a review
  question) and `..._email_indexing.py` (the email-indexing chain on the production graph: a page delivered twice at once, a lost next-page enqueue
  retried from the saved cursor). The scheduler, To Do and email-indexing files are `slow` (they build the production graph).
- **Scheduler types, table in the docstring of `test_worker_redelivery_scheduler.py`.** Eleven types, each pinned or ruled out; ten are pinned by a test here or by a named existing test:
  `fire_due_reminders` (one-time: here, sequential and gathered; recurrent: `test_reminders_due_instants.py`), `start_daily_email_review`,
  `sweep_consolidation`, `fact_lifecycle_sweep` and `start_drive_sync` (existing tests), `start_email_indexing` (here; strict xfail P15-D6),
  `email_indexing_watchdog` and `repair_email_embeddings` (here, sequential; the gathered run is harmless by construction, cited),
  `sweep_companion_consolidation` (the chain file) and `renew_all_task_subscriptions` (one renewal per user per run, by design). Ruled out: `billing_daily_summary` (not tested; P15-D8 is overstated, a second summary needs a manual double run).
- **Retry after an effect.** `tests/unit/handlers/test_worker_retry_after_effect.py`: the first attempt makes its effect, then answers 5xx or
  raises; the claim is released and the same task name is delivered again. Passing pins: a turn that reached no channel is retried and posts once; a
  lost video next-poll enqueue is retried and the chain continues once; a Telegram update or a Slack event whose turn failed after its reply is not run
  again; a question posted and recorded is not posted again.
- **Defects pinned as strict xfails** (the id is in each reason; the fix removes the marker). Each has a precondition that holds before and after the
  fix and a passing control in the same file:
  - P15-D1 (four tests, `test_worker_retry_after_effect.py`): a background turn that already posted or enqueued is answered 500, so the retry repeats it (a reminder or a daily review,
    a failed rich part after the text, a report enqueued before the failure).
  - P15-D3 (`test_worker_retry_after_effect.py`): a lost `execute_reminder` enqueue leaves the fire claimed and it is never enqueued again.
  - P15-D5 (two tests, `test_worker_redelivery_chains.py`): a companion batch is extracted by two concurrent deliveries; a batch that always fails is revived and extracted without bound.
  - P15-D6 (`test_worker_redelivery_scheduler.py`): two concurrent auto-index ticks both pass the running-job guard and start two jobs.
  - P15-D11 (`test_worker_retry_after_effect.py`): a review question whose post succeeded but whose message-ref write failed is posted again the next day, with a false "not delivered" note.
  - P15-D12 (`test_worker_redelivery_chains.py`): a concurrent double click on a review question applies the answer twice (a second owner-trail entry on the fact, doubled answer counters);
    the message the owner sees is correct. Low impact (audit trail and statistics).
- **Overstated ids, not xfails.** P15-D2 (gathered To Do setup: setup is enqueued once per OAuth callback, so two copies need two connects in one
  instant; the sequential double is pinned), P15-D4 (a landed-then-error enqueue chain: a rare deadline after the server accepted the task; the
  not-landed retry is pinned as a passing test), P15-D7 (a drive-sync fan-out race; existing tests pin the deduplication) and P15-D8 (a second billing
  summary after a manual double run) are not tested where today's behaviour is right, and each file's docstring says which.
- **Agent instance state.** `tests/unit/agents/test_agent_request_state_meta.py` with `agent_instance_writes_allowlist.txt` (23 lines): every write to
  shared state outside `__init__` in a class that derives from `BaseAgent` (`self.*`, class state, module-level names, `global`, `setattr`) needs a line
  with a reason and evidence, because agent instances are per-user singletons serving concurrent executions (the 3.6x billing inflation was such a
  write). Reasons in use: `constructor-only` (checked: every call is in `__init__`), `lazy-cache` (checked: each write is under `if self.x is None:`),
  `cross-request-by-design` (the per-user drive mutation registry, the billing and logger buffers), `cross-request-by-design+lock-guarded` and
  `lazy-cache+lock-guarded` (the lock is checked, and is never a reason alone), `port-call` (a counter on an injected port) and `dead-write`
  (`config.metadata["user_tone"]`, nothing reads it). It fails both ways; 38 tests, including self-tests per write shape. Known limits are in the file.
- **Mutation registry.** Eleven modules are registered in `tests/support/mutation_targets.py`: the worker handler, the task claim store, the reminders
  service, the note store, the To Do setup service, the companion extraction service and queue, and the four P16 modules the P16 merge did not register
  (recurrence adapter, account repository, session service, daily review window). Each has its own `*_equivalent_mutants.txt` holding one header comment, which the driver skips, so no mutant is ruled equivalent yet. Own files because
  `test_every_equivalent_file_belongs_to_a_registered_target` (`tests/unit/test_mutation_targets.py:44`) counts `None` as one shared file: two or more
  `None` targets fail it, and only the wiring file may be shared. The route test `test_worker_redelivery_route.py` reaches the worker handler and the
  claim store only through `src/`, so it is their `extra=`; `test_non_wiring_targets_do_not_get_the_graph_group` was amended by the final review to allow
  `extra` on a non-wiring target.
- **Coverage.** `make ci` at the end: 25 917 passed, 77 skipped, 106 xfailed; total 91.63 %. Per layer (branch-aware): domain 98.62, ports 92.94,
  infrastructure 96.70, agents 95.34, services 92.26, handlers 93.59, adapters 86.77, web 77.61, composition 94.55, src (other) 76.18, root entry
  points 76.63; against floors of 92.7 (handlers) and 85.7 (adapters), so the script offers 93.2 and 86.4. The floors are raised in this PR, after the CI "Coverage per layer" notice, by the rule min(local, CI) - 0.3
  (handlers at most 93.2, adapters at most 86.4). The package adds 10 of the suite's 106 strict xfails (D1 four, D3, D5 two, D6, D11, D12); the full run takes about 6 min 15 s.
  The `firestore_notification_state_adapter` allowlist line of `untested_modules_allowlist.txt` is deleted: the redelivery world imports it.
- **Found while building, not carded** (no defect id; each needs an arbiter decision):
  1. A reminder is posted again if writing its delivered stamp fails right after the post: `mark_fire_delivered` raising
     (`src/handlers/worker_handler.py:1043`) releases the claim, and the retry finds no stamp.
  2. Two reminders created in the same millisecond overwrite each other: the note id is the creation millisecond and the write is a plain `set()`
     (`src/adapters/firestore_agent_note_adapter.py:68,83`). Two `manage_self_reminders` calls in one tool batch run concurrently; likelihood unverified.
  3. Two renewals of one user's To Do subscription at once leak one subscription: both read the config, register a replacement and save, with no
     guard (`src/services/task_setup_service.py:174-208,290-318`); the other subscription stays on Graph until it expires.
  4. No test pinned the Telegram intake `dedup_key` (`src/adapters/telegram/webhook_adapter.py:146,154`); the chain file now does.

**Built (P11, 2026-10-27).** Every other stateful store, by inventory. Docstring changes in `src/` (27 ports, 26 adapters; no code change), the rest under `tests/`. Deviations and results:
- **The semantics inventory.** Four inventories (the stores in four groups by risk) were read at the callers, probed on H1 and accepted as the plan; a reviewer read every card before any kit. Each port docstring now states its contract on the seven axes (order, limit, legacy documents, tenancy, atomicity, idempotency, errors), with defect ids where a known defect sits on an axis. **Firestore and GCS mechanics** (collection names, TTL policy or its absence, composite indexes, write mechanics, search internals) are in the adapter class docstrings, not the ports, which keep the observable contract. Legacy-document shapes no writer produces are stated as "no writer produces one; behaviour pending the data audit" and are not tested.
- **The kits** (`tests/unit/adapters/`, real adapters on H1 with the index refusal on, except the GCS and Cloud Tasks kits; each file's docstring names the port and what it leaves out):
  - Voice, dedup and billing: `test_ephemeral_store_kit.py`, `test_dedup_store_kit.py`, `test_task_claim_store_kit.py` (legacy shapes, the done window, lost writes), `test_agent_note_store_kit.py`, `test_account_store_kit.py`, `test_quota_service_kit.py` (over the real account repository), `test_long_turn_store_kit.py`.
  - Session and queues: `test_session_store_kit.py`, `test_consolidation_queue_store_kit.py`, `test_companion_extraction_queue_store_kit.py`, `test_indexed_email_store_kit.py`, `test_user_store_kit.py`, `test_notification_state_store_kit.py`.
  - Small stores: `test_task_search_index_store_kit.py`, `test_task_config_store_kit.py`, `test_channel_binding_store_kit.py`, `test_skill_store_kit.py`, `test_memory_fact_draft_store_kit.py`, `test_companion_memory_store_kit.py`, `test_companion_cache_store_kit.py`, `test_email_job_store_kit.py`, `test_email_exclusions_store_kit.py`, `test_short_link_store_kit.py`.
  - Non-Firestore: `test_workspace_store_kit.py`, `test_file_storage_store_kit.py`, `test_media_store_kit.py` (the three GCS adapters on the shared GCS fake) and `test_task_queue_store_kit.py` (`GcpTaskQueue` over the Cloud Tasks client fake of `tests/support/redelivery_world.py`, validating each request with the SDK's own request type).
- **Caller pins** sit in files named after the caller, because the defect is in the caller and the store only exposes it; each runs the real caller over the real store: `tests/unit/services/test_voice_call_setup_service_store_pins.py`, `test_overflow_routing_service_store_pins.py`, `test_language_preference_service_store_pins.py` (with the sign-in refresh in `AuthenticationService`, a `Rendezvous` after both reads), `test_long_turn_service_store_pins.py`, `test_task_setup_service_store_pins.py`, `test_email_indexing_service_store_pins.py`, `test_rich_content_service_store_pins.py`; `tests/unit/handlers/test_conversation_handler_store_pins.py`; `tests/unit/test_job_main_store_pins.py`. Each has a passing control beside the xfail.
- **The H2 meta-test.** `tests/support/store_inventory.py` (the one definition: AST discovery of every public port-implementing class in the store modules of `src/adapters/`, `KITS`, `CLIENT_FAKES`, `BUILDERS`) and `tests/unit/adapters/test_every_stateful_store_has_a_kit.py`: each of the 43 classes is in `KITS` xor on `tests/unit/adapters/stateful_store_allowlist.txt`; each kit file imports the class and constructs it over a fake with semantics (a mock client does not count) or calls a named builder; stale allowlist lines and `KITS` typos fail. The P4, P6 and S2-S4 suites and the two drive repositories' contract suites are registered as they stand (38 `KITS` entries). Five allowlist lines remain:
  - `FirestoreIAMAdapter`: no caller in `src/`; its test uses a mocked account repository.
  - The four prompt stores (component repository, agent profile, blueprint, token): P10.
- **H1 change (card H).** A naive `datetime` in a query filter value (also inside an `in` or `array_contains_any` list) is the UTC instant, as the real client encodes it; it used to raise `TypeError` against stored aware values. Cursor values are compared as given; `DatetimeWithNanoseconds` is unchanged (the kits seed raw documents). New parity scenario `naive_datetime_filter_is_utc` (`tests/support/firestore_parity.py`): agreed on the emulator 2026-10-27, not yet on the test database (marked in the module header).
- **The GCS fake** (`tests/support/gcs_fake.py`, self-tests `tests/unit/support/test_gcs_fake.py`, 38 tests; adapters get it as the injected client or through `install(monkeypatch)`). Models: declared plain or versioned buckets (undeclared: `NotFound`), strictly increasing generations, `if_generation_match` (0 = no live version), response-loaded blobs (a delete through a blob that carries a generation erases that version, the trap the workspace adapter is built around), only the writable properties sent by an upload or patch, `patch` metadata semantics and `custom_time` that cannot move back, `NotFound` on a missing object, lexicographic listing, object-name limits, the injected clock, deterministic signed URLs, one lock per request, faults and interleavings (`fail_next(op, exc, landed=)`, `on(op, hook, when=)`, hooks outside the lock). Refuses: any access change (`make_public`, `acl`, `predefined_acl`), a signed URL over 7 days, an unmodelled or misspelt SDK parameter (`TypeError`). **Unconfirmed behaviours, marked `# P12 verifies`:** a noncurrent version addressed by generation, a conditional delete of a missing object (404, not 412), the order of the versions of one name in a listing; the error class of an over-long name is the fourth open scenario. `tests/support/gcs_parity.py` holds 18 scenarios run on the fake in `make check` (`tests/unit/support/test_gcs_parity.py`) and on the versioned test bucket in `tests/cloud/test_gcs_parity_cloud.py`, which skips without `TEST_GCS_BUCKET` (and without the cloud credentials); the cloud run and the bucket are P12's.
- **Mutation.** 23 store modules are registered in `tests/support/mutation_targets.py`, each with an `*_equivalent_mutants.txt` that starts empty; the weekly semantic run is the first pass (no local mutation pass), so no survivor count exists yet.
- **Results.** 27 new kit files and 9 pin files, 388 test functions in the kits and 21 in the pins (`grep -c` of `def test_`), the meta-test 11, the fake 38. **18 defects pinned as strict xfails, 20 tests** (10 in kits, 10 in pin files; the id is in each reason; the fix removes the marker). No fix is in this package; fixes are outside it.
  - Voice, billing, notes: P11-G1-1 two concurrent claims both pass the one-call marker (the `voice_auth` route has the same check-then-set); P11-G1-2 two notes created in one millisecond overwrite each other (note id = creation millisecond, plain `set`); P11-G1-3 `update_account` overwrites usage counters incremented since the caller's read; P11-G1-4 a deep-research job increments usage directly and its daily-limit crossing is never announced (`job_main`).
  - Session, email, user: P11-G2-1 re-saving a consolidated email clears `consolidated_at`; P11-G2-2 every vector query failing reads as no match; P11-G2-3 an overflow batch is cut before its hand-off and lost when the enqueue fails (the known loss in the memory map); P11-G2-4 concurrent profile updates lose one (whole-profile write, no precondition); P11-G2-5 the chat-turn save, `LongTurnService.save_late_answer` and `LongTurnService.mark` retry only an exception, but the session store swallows a transient error by default, so the retry never runs (three tests).
  - Small stores: P11-G3-1 every vector query failing answers `[]` ("no tasks"); P11-G3-2 a subscription without `expires_at` reads as an aware now where every caller compares with a naive now; P11-G3-3 `ensure_subscriptions` saves a stale config over the list id it just stored; P11-G3-5 the email-indexing watchdog overwrites a job that completed after its stale query. Not pinned by decision: the companion summary cache is keyed by session only while the companion records are scoped by account; the companion stores are to be reworked with the agent workspace, and the case is tested then.
  - GCS: P11-G4-1 a workspace delete that loses to an overwrite leaves its stamp on the replaced version; P11-G4-2 two concurrent chat uploads of one name both return it and the later replaces the earlier; P11-G4-3 a delete of a missing upload leaks the SDK `NotFound`; P11-G4-4 a fetch of a missing media key leaks it too; P11-G4-5 an HTML rich-content file is posted as its storage key. The second residual of G4-1 (the workspace RFC's accepted case) is not pinned.
  - Cloud Tasks: none.
- **Not covered.** Legacy document shapes (notes, accounts, sessions, batches, emails, users, NFD file names): left for a read-only data audit. Methods with no caller in `src/` (`GcsFileStorageAdapter.get_url`, `GcpTaskQueue.get_queue_stats` and `purge_queue`, `FirestoreTaskConfigRepository._strip_tz`, the user-id argument of `delete_exclusion`, the session store's `append_message`, `get_latest_session_id`, `delete_session` and `cleanup_expired_sessions`): the docstrings say "no caller" and the kits test no more of them than the contract needs. The fact review store runs only through the review world's suite, the drive index repository through its v2 parity suite. The meta-test does not require the index refusal on a kit's `EvalDb` (it checks the fake, not its options; the Firestore kits turn the refusal on by themselves).

**Rules revised (owner, 2026-10-27).** The owner reviewed the programme: every package stays and is built now (no "on touch"
deferral); what drops is the friction a gate adds to every feature change, and the programme must stay manageable over years
without his attention. Everything that can run in the cloud runs in the cloud; the two weekly workflows never report to Slack (the CI, timing and PII-gate notifications are unchanged); artifacts are read by a
scheduled routine that fixes survivors by PR.
- **F1 `.github/workflows/mutation.yml`** (`Mutation survivors`): Sunday 02:00 UTC and `workflow_dispatch` (optional input `module`);
  one job per module registered in `tests/support/mutation_targets.py` (`MutationTarget(module, equivalent, operators="default", graph_group=False, extra=())`, `TARGETS`; tests are computed by import closure, the six wiring targets add the graph group (`graph_tests()`),
  `extra` is the escape hatch for a test that reaches the module only through `src` or a conftest fixture;
  CLI `python -m tests.support.mutation_targets --matrix | --tests-for <module> | --summarize DIR --out-md --out-json | --annotations survivors.json`). The driver gained `--report PATH` (JSON)
  and `--no-fail`. Artifacts `mutation-<slug>` per module and `survivors` (`survivors.md`, `survivors.json`), also in the step summary.
  The `summarize` job also prints the verdict as check-run annotations (a notice per module, warnings for survivors, at most 9, 4000 characters each). `survivors.md` lists every registered module, `missing` when a job produced no report and `partial (N of M)` when it was cut (the report is rewritten after each mutant). Never red because mutants survived. Registering a package's modules in `TARGETS` is part of "package done"; a unit test fails when a
  `*_equivalent_mutants.txt` file has no registered target. The local run (the table in `tests/CLAUDE.md`) stays as a tool for anyone to run by hand, optional (see the 2026-10-27 line below).
- **F2** `UPDATE_PINS=1 RUN_SLOW_TESTS=1 pytest tests/unit/test_main_boots_offline.py` rewrites `tests/unit/main_boots_offline_url_map.txt`
  from the booted app (header kept, sorted); the diff in the PR is the review. Without the variable nothing is written. The implementer commits the rewritten pin, the task reviewer reads its diff (no separate approval), and a new route must also be refused or listed in `PUBLIC` of `tests/unit/web/test_every_route_refuses_anonymous.py`.
- **F3** Docs-only PRs skip `make ci`. `scripts/ci/docs_only.py` is the one definition of "docs": every file under `docs/` (any extension), root `*.md`,
  `.github/**/*.md`, `CLAUDE.md` anywhere except under `src/skills/`; nothing else (not `src/**/SKILL.md`, `tests/fixtures/**`, `scripts/**`, any
  other file). The skip also applies to a push to `main` and to the `timing` job; dependencies are still installed, the PII gate and the CI Slack
  notice still run. The `check` job keeps its name and prints a notice instead; a docs-only PR still runs the doc-reading tests listed in
  `DOC_READING_TESTS` (the meta-test is an equality check; known gap: a test reading a root `*.md` or `CLAUDE.md` is not detected).
- **F4 `.github/workflows/parity.yml`** (`Firestore parity`): Sunday 03:00 UTC and `workflow_dispatch`; authenticates through Workload
  Identity Federation as the test service account (`tests/cloud/conftest.py` accepts default credentials that already identify as that
  account, otherwise impersonates as before; every guard unchanged); runs `make test-cloud`; artifact `parity` (`parity.xml`,
  `parity.log`; four values are replaced before upload: the three secrets plus `TEST_WIF_PROVIDER` and the ids inside it; a failed redaction deletes both files and skips the upload). Never red because a scenario failed, but red when a secret is empty, federation fails, zero tests ran, or `parity.xml` is missing or unreadable. The job (named `parity`) also publishes its verdict as annotations: counts, and the names of failed tests.
- **F6 (outside the repo)** the Monday routine, a scheduled cloud Claude session: reads the check-run annotations of the `summarize` job (mutation)
  and the `parity` job via `GET /repos/{o}/{r}/check-runs/{job_id}/annotations` (a cloud session cannot download artifacts or job logs; artifacts stay for the owner and the Mac), writes the killing assertion or proposes an `equivalent` line per survivor, opens a test-only PR. KPI: open survivors by
  the next Monday = 0, `error` statuses = 0, growth of equivalents watched. It escalates to the owner only a survivor older than two
  weeks, in one line with a recommendation.
- **R7** Mechanical adaptations of existing tests after a rename or a signature or constructor-argument change go in the implementer's
  own commit titled `test(mechanical): ...` (an import path fixed after a rename needs no reviewer). The reviewer reads that commit as a
  whole and checks one thing: no assertion changed (a different value, condition or count asserted; renaming an identifier inside an assert is mechanical). Behavioural edits stay per test under the reviewer rule.
- **R8** In `wiring_instance_ledger.txt` a line with count 1 is a registration and is the implementer's to add; counts above 1 and every
  line of `wiring_none_ledger.txt` remain the reviewer's decision. Both ledgers still fail both ways.
- **R9** A per-layer coverage floor (`scripts/check_coverage_layers.py` `FLOORS`) may be lowered in the same PR that needs it, with a
  one-line reason comment next to the value and the reviewer's OK; the owner reads it in the diff. Floors never drift down silently.
- **Drops and conditions.** P23 (weekly `mutmut`) is dropped: the weekly semantic run is the mutation metric. P12 keeps its remaining
  content but drops the lease document (one weekly run under a `concurrency` group) and the render step as a gate. P17 is accepted only
  with a regeneration command per recorded fixture (`make contract-record PROVIDER=...`), never hand-edited fixtures. A local pre-push
  hook for `slow` tests was considered and dropped (CI is the gate).
- **Rejected.** Deferring packages to "when the store is next touched" (the owner will not interrupt feature work later); survivor
  reports to Slack (nobody triages daily); a mutation run on every merge to `main` (composition modules change in almost every feature
  PR, 1-5 h each; reconsider if survivors accumulate between weekly runs). Record: `decisions/test_gates_friction.md`.
- **2026-10-27 owner: the per-package mutation done-check is dropped** (and the P9 wiring done-check with it); P15 part 2 was the last package
  with one (31 survivors: 24 test gaps, 7 equivalent, 0 code bugs). Registering the modules in `TARGETS` stays part of "package done"; the
  driver and `--tests-for` stay as tools; the weekly run is the only mutation pass. Record: `decisions/test_gates_friction.md`.

**Totals.** Tranche 1 (P1–P9) ≈ 12–16 days; tranche 2 (P10–P17) ≈ 24–33; tranche 3 (P18–P24) ≈ 9–11. **Whole plan
≈ 45–59 days.**

How the reviewers' figures relate: rev 1 said 19–21 days. The feasibility review re-estimated rev 1's scope at
≈ 33–45 days (first block WP12 + WP0–WP3 ≈ 9–12). The gaps review added new scope at rev 1's rates (≈ 31–34 total, +≈12
days of new packages). Applying both — the feasibility rates and the gaps scope — gives the totals above. The first
tranche is larger than 9–12 because the arbitration moved the emulator spike, the parity slice, the inbound gates and
reachability into it. P17 alone is about a quarter of the plan.

## 6. Execution

- Order: tranche 1 strictly in the order P1 → P2 → P3 → P4 → P5 → P6 + P7 → P8 → P9. No kit other than the fact kit is
  built on H1 before P5 has run.
- Subagents per package from a precise brief (Sonnet for mechanical suites with a clear done-criterion; Opus for the
  H2 semantics inventories, P6, P7, P19 and every review), each in its own worktree with `.testmondata` seeded; I
  verify and integrate (`feedback_code_via_subagents`).
- Every package: new tests only; any existing test found to encode a defect goes to a reviewer with the failing
  assertion and the spec (CLAUDE.md reviewer rule).
- Defects found while writing the tests are fixed in the same package when obvious, else listed for the owner with
  context. The §1.4 defects are fixed in their own urgent session first.
- Each package's report states coverage before/after (branch), the inventory meta-test delta, and the semantic
  mutation result.

## 7. Owner decisions (2026-10-21)

Package names in brackets map rev 1's numbers to this revision.

1. **Cloud gate (§4): yes.**
2. **Network guard in `make check` (H3): yes.**
3. **Coverage floor in CI (WP0 → P2): yes** — per layer at today's values, only rising; a source file no test imports
   fails. (Rev 2 implements the second half as a static check with a shrinking allowlist, since coverage cannot detect
   it and a hard failure on day one would turn CI red.)
4. **Order: yes** — WP12 + WP0–WP3 first (P1, P2, P4, P6, P8), then WP4–WP7, then the rest. Work starts the week of
   2026-10-26. (Rev 2 inserts the spike, the parity slice, the inbound gates and reachability into the first tranche;
   see Owner confirmations 1.)
5. **Reality checks (§4a): yes** — spec drift weekly, contract review with Claude monthly, probes where a sandbox exists
   or the write cleans itself up. The Slack developer sandbox and the Telegram test-environment bot are set up by the
   owner when WP7b (P18) starts.
6. **LLM metadata reads in probes: allowed** (`models.list` / `retrieve`; never a generation call).
7. **Budget:** 3 minutes is for local runs; CI may take longer (§4b).

## Owner confirmations (both confirmed 2026-10-21)

1. **Realistic effort: ≈ 45–59 days in all; first tranche ≈ 12–16.** Context: rev 1 said 19–21. The feasibility review
   put rev 1's own scope at ≈ 33–45 (first block ≈ 9–12); the gaps review added about 12 days of new packages
   (stores by inventory, service seams, scripted end to end, inbound gates, reachability, redelivery, clocks, data
   audit); the arbitration accepted both and moved four items into the first tranche. Recommendation: confirm the
   first tranche as ordered (it closes access gates, the fact store, the fake's validity and reachability, and changes
   the convention so new code stops adding to the gap), and decide the scope of P17 when tranche 2 starts: it is about
   a quarter of the plan, and the cheap part (recorded fixtures, Stainless param-type validation) gives most of its
   value.
2. **The test database, service account and identity setup are needed in tranche 1, not at the end.** Context: P5
   (the parity slice) needs the test Firestore database, the test service account with `datastore.user` under a
   database-scoped IAM condition, the impersonation grant (`serviceAccountTokenCreator` on that SA for your user), and
   the composite and vector indexes created from the rendered definitions (`datastore.user` cannot create indexes).
   CI's post-merge job additionally needs Workload Identity Federation limited to `main`. All of these are owner infra
   steps that widen access, so I do not run them. Recommendation: do the database, SA, impersonation grant and index
   creation before P5 (about half an hour, from a script I prepare that reads every name from `.env`); defer WIF to
   P12, because the parity slice runs locally first. P3 showed the emulator covers all probes, vector search included,
   so only the index refusals and the three undocumented points wait on this step.

   **Done 2026-10-21** with the owner's OK, by `scripts/infrastructure/setup_test_gate.sh` (idempotent, dry run by
   default, every name from `.env` `TEST_*`): the test Firestore database; the test service account holding
   `datastore.user` and `datastore.indexAdmin` under an IAM condition on the test database only (so index creation in
   the test database needs no owner step); the owner's impersonation grant; a versioned test bucket with a 1-day
   lifecycle; a paused test queue; and Workload Identity for CI on `main` (repository secrets `TEST_WIF_PROVIDER`,
   `TEST_SA_EMAIL`). Verified: the impersonated account writes and reads the test database and is refused
   (`PermissionDenied`) on the live database.

## Appendix. Execution order across this RFC and FACT_LIFECYCLE_RFC (owner-approved, 2026-10-21)

Two streams at most (one machine; parallel full suites choke each other).

| Phase | Stream | Items, in order |
|---|---|---|
| 0 | — | the urgent defect session for §1.4 (in flight); nobody else touches the worker handler, the dedup store or the consolidation queue until it is merged |
| 1 | — | P1 → P2 → P3; the owner's test-infrastructure step |
| 2 | A, memory | P4 → P5 → P8 (Consolidation + Tasks) → P21 → FACT_LIFECYCLE_RFC S2 → S3 → S4 → S5 |
| 2 | B, security and breadth | P6 → P7 → P9 → P15 + P16 (P9, P15, P16 after the defect session is merged) → P11 → P13 + P14 → P10 → the cheap part of P18 (spec-drift workflow, monthly contract-review skill) |
| 3 | — | P12 → P19 + P20 → P22 + P23 + P24 |
| 4 | — | P17 (start with OpenAI and Claude, then Gmail and Graph), P18 live probes (owner sandboxes) |

The live status of every item is kept in the project's working notes, not in this file.
