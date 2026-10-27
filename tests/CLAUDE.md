# Tests

pytest + pytest-asyncio, `asyncio_mode=auto`. Why these rules: `docs/10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md`.

## Running — which command when

This table is the one place that says which test command to run when. Other docs point here.

| When | Command | What it runs |
|---|---|---|
| While editing | `pytest <path>::<test>` or `make test-affected` | the tests you name, or the unit tests testmon selects for your diff (it can miss a test whose input is a data file or code in a child process) |
| An intermediate commit | `make test-affected` + the tests of the files you changed; **when the commit touches a data file or code run in a child process, also that folder's tests** (e.g. `pytest tests/unit/prompts` for `prompts_snapshot/`) | what testmon selects, plus the tests it cannot see |
| Before a push | `make check`, **once per push**, on the branch head (not per commit; owner, 2026-10-27) | lint + mypy + the unit suite without `slow` tests + the offline G2 test |
| Before you call a package done, and whenever you report coverage | `make ci` | everything in `make check` + `slow` tests + the coverage gates (per file and per layer); CI runs exactly this on every PR |
| Mutation check, by hand, optional | `python -m tests.support.semantic_mutants --module <m> --tests $(python -m tests.support.mutation_targets --tests-for <m>) [--operators wiring] [--equivalent <file>]` | one module's mutants; nobody has to run it: the weekly run is the only mutation pass ("Mutation check" below; wiring command in "The production graph") |
| Only when the task needs them | `make test-integration`, `make test-e2e-all` | some integration tests and all e2e tests call real services |
| Fake against the real thing (P5) | `FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 pytest tests/unit/support/test_firestore_parity.py`, and `make test-cloud` | the Firestore parity scenarios on the emulator and on the test database; the GCS scenarios run on the fake in `make check` (`tests/unit/support/test_gcs_parity.py`) and, with `TEST_GCS_BUCKET` set, on the test bucket in `make test-cloud` (see below) |
| Docs-only PR | (automatic) | `make ci` is skipped (F3; also on a push to `main` and in the `timing` job; dependencies are still installed, the PII gate and the CI Slack notice still run). Docs: every file under `docs/` (any extension), `CLAUDE.md` anywhere except under `src/skills/` (nothing there is docs), root `*.md`, `.github/**/*.md`. The doc-reading tests listed in `scripts/ci/docs_only.py` (`DOC_READING_TESTS`) still run; the meta-test is an equality check (fails when an unlisted test reads docs and when a listed one stops). Known gap: a test reading a root `*.md` or a `CLAUDE.md` is not detected |
| Weekly cloud runs (nothing to do by hand) | `Mutation survivors` (`mutation.yml`) and `Firestore parity` (`parity.yml`), Sunday; after a package: `gh workflow run mutation.yml -f module=<module>` (works once the module is registered on `main`, or with `--ref <branch>`; `Firestore parity` runs on `main` only, WIF is bound to it) | artifacts `survivors` and `parity` and as check-run annotations (≤ 10 per step, 4000 chars each) for the cloud reader, read by the Monday routine, which fixes survivors by PR; never red for a survivor or a failed scenario, never Slack |
| Re-pin the URL map | `UPDATE_PINS=1 RUN_SLOW_TESTS=1 pytest tests/unit/test_main_boots_offline.py` | single file, no `-n`; rewrites `main_boots_offline_url_map.txt` from the booted app; the implementer commits the rewritten pin and the task reviewer reads its diff (no separate approval); a new route must also be refused or listed in `PUBLIC` of `tests/unit/web/test_every_route_refuses_anonymous.py` |
| Wall-clock tests | `make test-timing`, by hand (CI runs it after a merge to `main`) | `timing` tests only |

Never: re-run a suite because a `timing` test failed; combine `--testmon` with `--cov`; run two full suites at once
(the lock below makes the second wait).

```
tests/
  conftest.py     — fixtures + the plugin in tests/plugins/ (suite lock)
  plugins/        — pytest plugins: the suite lock, `network_guard.py` (the offline G2 run)
  unit/           — offline, no real service. Fakes and in-memory repositories. conftest.py = the network guard
  integration/    — mocked or scripted services; the offline G2 end-to-end test runs in `make check`
  support/        — shared fakes (fact_store.py, firestore_eval.py), network_guard.py, plugin_runs.py, semantic_mutants/
  contracts/      — adapter contract rules (adapter_contracts.py)
  cloud/          — the cloud gate: real writes to the Firestore test database and, with `TEST_GCS_BUCKET` set, the GCS test bucket (`make test-cloud`); never in check or ci, weekly in the cloud (`Firestore parity`, Firestore only until P12)
```

## The convention

- **Fakes with semantics over call assertions.** A test of a store or a service asserts what ends up stored and what a
  reader returns, against a fake that evaluates (`tests/support/firestore_eval.py`, `tests/support/fact_store.py`,
  `tests/unit/adapters/firestore_drive_eval_fakes.py`, the in-memory repositories). `AsyncMock(spec=Port)` plus
  `assert_called_once()` proves the code called the neighbour, never that the neighbour would do the right thing; it
  is how a dropped filter, a flipped sort and a lost field stayed green. Use a mock for a boundary you do not own
  and only to script its answer; assert the outcome, not the call. Same for services above the ports: do not mock an
  internal service without a spec.
- **An adapter needs a behind-port test at its boundary** (SDK, HTTP or database), per
  `docs/how_to/ADAPTER_WIRE_TESTING.md`: mock the SDK object, not the port, and pin the wire format. Line coverage
  produced by a `MagicMock` SDK is not evidence of behaviour: a reviewer does not accept it as the adapter's test, although the
  floor still counts it.
- **The aiohttp boundary has one fake.** A test that needs the HTTP surface the To Do, Gmail and Slack webhook adapters use takes
  `tests/support/http_boundary_fake.py` (`install(monkeypatch)`, exact-path routes, unrouted URLs fail loudly), never a new fake.
- **No test calls a real LLM**, and no unit test touches the network (below). A test that needs the real service is an
  integration test and lives in `tests/integration/`.
- One test class per service or agent. `@pytest.mark.requirement("REQ-XXX")` for business requirements.
  Arrange-Act-Assert.

```python
async def test_content_update_writes_a_new_version(store):        # store = FactStore(): real adapter, repo and service on a fake Firestore
    fid = await _create(store)
    res = await store.adapter.update_fact(fid, {"content": "lives in Springfield"}, ACC)
    new = store.docs[res["fact_id"]]                                # assert on the raw stored documents, not on a mock call
    assert new["text"] == "lives in Springfield" and new["version"] == 2
    assert store.docs[fid]["superseded_by"] == res["fact_id"]       # the old document is closed in the same commit
```

## Invented cast

The repository is public (root `CLAUDE.md` -> "PERSONAL DATA RULE"). An example about a person's life (a name, a
relative, a place, a document, a number, a contact) uses only the owner's invented universe; its location is given in
the session setup, and nothing from it is copied into this repository. A new fictional element is added to the universe
first, then used. Nothing comes from real life, memory, journals, chat history or `get_user_context`, not even with the
names swapped. Contacts use reserved ranges only (`example.com`/`.org`/`.net`, `.test`, `.invalid`, `+1 555 01xx`).

Tests of the personal-data gate use invented markers of their own (`tests/unit/scripts/test_pii_*.py`).

## The network guard and the gRPC tripwire (H3)

Every test under `tests/unit/` (autouse fixture in `tests/unit/conftest.py`, `tests/support/network_guard.py`; it covers test bodies and
function-scoped fixtures, while module/session fixture setup and import-time code are not guarded — keep them fake-only) runs with
`connect()` to anything but `127.0.0.1`/`::1`/a unix socket refused (`pytest-socket`; creating and binding sockets
stays allowed, so asyncio loops and loopback servers work) and with a tripwire on `google.auth.default`,
`grpc.secure_channel` and `grpc.aio.secure_channel` unless the target is localhost (Firestore talks through gRPC's
own sockets, which the socket guard does not see). The offline G2 test gets the same guard from
`-p tests.plugins.network_guard` in `make test-g2-offline`. When it fires: use a fake, a loopback server, or move the
test to `tests/integration/`. Do not disable the guard in a unit test. **Out of scope, by decision:** subprocess
children (drive parser, sandbox engine, ffmpeg).

## Fake against the real thing: the parity scenarios (P5)

`tests/support/firestore_parity.py` holds one set of scenarios, each with ONE documented expected value, run on three backends:
H1 in `make check` (`tests/unit/support/test_firestore_parity.py`),
the Firestore emulator in the same file when `FIRESTORE_EMULATOR_HOST` is set (`npx -y firebase-tools@latest setup:emulators:firestore`,
then `java -jar ~/.cache/firebase/emulators/cloud-firestore-emulator-v*.jar --host=127.0.0.1 --port=8085`; it does not refuse a
query for a missing index, so those scenarios skip), and the test database in `tests/cloud/` (`make test-cloud`). The test database
is the arbiter. A new kit that leans on a Firestore behaviour H1 has not been checked for adds a scenario here first. The cloud
run (switched on by `RUN_CLOUD_TESTS=1`, which `make test-cloud` sets; a bare pytest skips it) reads `TEST_FIRESTORE_DATABASE`, `TEST_SA_EMAIL` and `GOOGLE_CLOUD_PROJECT` from the environment or the repo's dotenv file,
refuses to start unless the database ends in `-test`, is not the live one and the client uses the test service account's
impersonated credentials, and creates the scenarios' indexes once (up to 15 minutes; a run that creates indexes may fail scenarios of those collections even after READY (seen once, cause unknown): re-run). The weekly `Firestore parity` workflow (`.github/workflows/parity.yml`, Sunday 03:00 UTC and `workflow_dispatch`) authenticates through Workload
Identity Federation as the test service account and runs `make test-cloud` (artifact `parity`: `parity.xml`, `parity.log`; never red for a failed scenario).
`tests/cloud/conftest.py` accepts default credentials that already identify as the test service account, otherwise it impersonates as before; every guard
is unchanged. The parity artifact is redacted (four values are replaced before upload: the three secrets plus `TEST_WIF_PROVIDER` and the ids inside it); the job is red when a secret is empty, federation fails, zero tests ran, or `parity.xml` is missing or unreadable; when the redaction itself fails both files are deleted and the upload is skipped. The Sunday 03:00 UTC run can collide with a manual `make test-cloud` (no lease yet): do not run it by hand at that time. One runner at a time: the workflow runs under a `concurrency` group (no lease document).

**GCS has its own set** (`tests/support/gcs_parity.py`, run against the fake in `tests/support/gcs_fake.py`): on the fake in `make check` (`tests/unit/support/test_gcs_parity.py`), and on a versioned test bucket in `make test-cloud` through `tests/cloud/test_gcs_parity_cloud.py`, which skips without `TEST_GCS_BUCKET` (the weekly workflow does not pass it yet: P12). A new GCS feature = a new scenario in `tests/support/gcs_parity.py`.

Extending the fake with a new Firestore feature = a new scenario in `tests/support/firestore_parity.py`, run on the emulator and with `make test-cloud`; an adapter that starts using a feature the fake lacks fails loudly (unsupported operator / no such method) — extend the fake then, never work around it in the test.

## Store kits (P11)

- **The rule.** Every public class of a store module in `src/adapters/` that implements a port (`firestore_*`, `gcs_*`, `gcp_task_queue`,
  `fact_management_adapter`, the `*_queue` modules, `prompt_v3/firestore_*`) is run by a kit over a fake with semantics (`EvalDb`, the GCS
  fake, the Cloud Tasks client fake) or sits on the allowlist with a reason. A second rule goes by import: a module of `src/adapters/` that imports a `google.cloud` firestore, storage, tasks or bigquery client either matches those names or is on `NOT_STORE_MODULES` with a reason. Lines only leave the allowlist; a new line needs the owner's OK.
- **The two files.** `tests/support/store_inventory.py` is the one definition: the discovery (AST, no adapter imported), `KITS` (class -> kit
  files), `CLIENT_FAKES` and `BUILDERS` (how a kit's source is recognised). `tests/unit/adapters/stateful_store_allowlist.txt` holds
  `module.Class  # reason`. `test_every_stateful_store_has_a_kit.py` fails a class in neither or both, a kit that imports the class but builds it
  over no fake, or over a mock (`MagicMock()`, a default `patch`, `adapter._client = MagicMock()`: a mock for a configuration argument next to a fake client is fine), a stale allowlist line and a `KITS` key that names no store.
- **A new store** gets its kit in the same change and one `KITS` line, not an allowlist line (a new line needs the owner's OK).
  A kit that builds its store through a new helper or fake adds its name to `CLIENT_FAKES` or `BUILDERS`, never an edit of the rule.

## Coverage floors and the untested-module list

- `.coveragerc` has `branch = True` and measures `src/` plus the root `*_main.py`; `make ci` fails under the global
  `--cov-fail-under`, the per-file table (`scripts/check_coverage_thresholds.py`) and the **per-layer floors**
  (`scripts/check_coverage_layers.py`, branch-aware percent per layer). Floors rise, or are lowered with a reason line and the reviewer's OK: raise a floor only to min(local `make ci`, the CI 'Coverage per layer' notice) − 0.3. The script
  prints "floor can rise to X" when that would be 0.5 above the floor; raise it in the same change. A floor may be lowered in the same PR that needs it, with a one-line reason comment next to the value and the reviewer's OK; the owner reads it in the diff (R9).
- The per-file table holds the auth files (verifiers, sign-in, MCP authorization, the gated webhooks and routes) at 100 % lines (P7):
  a refusal line no test runs fails `make ci`.
- `tests/unit/web/test_every_route_refuses_anonymous.py` probes every route of every deployed blueprint with an anonymous request: a new
  route or blueprint must be refused (nothing touched or written) or listed in its `PUBLIC` with a reason.
- `tests/unit/test_every_src_module_is_imported_by_a_test.py`: every module under `src/` and every root `*_main.py` is
  imported by some test, or listed with a reason in `tests/unit/untested_modules_allowlist.txt`. A new module needs a
  test, not an allowlist line. The list only shrinks: the test fails when a listed module is now imported. The check is `slow`,
  so it fails only in `make ci`.

## The production graph: wiring and reachability (P9)

The main service's non-web objects are built by `build_application_graph(config, db_client)` (`src/composition/application_graph.py`);
`main()` calls the same function, so a test that builds it tests production's wiring, not a copy.

- **The sealed environment** (`tests/support/prod_env.py`): `seal()` empties `os.environ` to a keep-list, then
  `apply_deploy_env()` sets what the `alek-bot-dev` step of `cloudbuild-dev.yaml` sets (its `--set-env-vars`, its `--set-secrets`
  names with shaped fakes, `K_SERVICE`, the build substitutions) and the real `load_settings()` runs. The `.env` file is never read.
  A test that needs the deploy's values uses this, never a hand-made `config` dict.
- **The fixture** (`prod_graph`, imported from `tests/support/prod_graph.py`): the built and started graph over the H1 fake and a recording Cloud Tasks
  client, with one user's eager and lazy agents built. Use it for any "is X wired in production" question.
- **Which tests are slow, and what that means for you.** The tests that build the graph (each builds it, starts it and builds one user's
  eager and every lazy agent) or boot `main()` (and send every trigger) are `slow`; most source-reading guards are fast (the execution-context
  test, the main.py construction allowlist, the checker self-tests); a guard that parses all of `src/` is `slow` (the enqueue-site tests of
  `test_worker_triggers.py` and `test_worker_payload_fields.py`). A forgotten dependency therefore shows up as an unledgered `None`
  in `make ci` or with `RUN_SLOW_TESTS=1`, **not in `make check`**; a bare `pytest <file>` skips those tests silently.
- **Producer and consumer payloads** (`tests/unit/test_worker_payload_fields.py`) tie every `/worker` producer to its consumer's reads: a new task
  type or payload key is added on both sides or listed in the test's allowlist with a reason.
- **The boot test** (`tests/unit/test_main_boots_offline.py`): runs the real `main()` offline, pins the full `url_map`
  (`main_boots_offline_url_map.txt`) and the keywords of every blueprint factory call, and sends every trigger through `/worker`
  against a pinned per-type table. Five rows are seeded in `_probe` (`_seed_rows`); seeding stays in `_probe` because
  `test_worker_redelivery_route.py` replaces it and needs unseeded rows. `tests/unit/test_worker_triggers.py` ties the scheduler script and the enqueue sites to
  `WorkerHandler`'s branches; `tests/unit/test_main_construction_allowlist.py` limits what `main.py` may construct itself.
- **The ledgers** (`tests/unit/composition/wiring_none_ledger.txt`, `wiring_instance_ledger.txt`; checks in `tests/support/wiring_ledger.py`):
  every `None` the walk finds and every instance count is listed with a reason; both fail **both ways** (an unlisted `None` or count,
  and a listed line whose `None` or count is gone). The reason is one of `off-in-prod`, `optional-knob`, `lazy`, `runtime`,
  `by-design`, `defect:D<n>` (the id from the P9 defect list), followed by evidence of at least two words. A count-1 line of `wiring_instance_ledger.txt` is a registration and
  is the implementer's to add; counts above 1 and every line of `wiring_none_ledger.txt` are the reviewer's decision, not a way to make a
  test pass. A `defect:` line is a wiring fault someone owns. `UPDATE_PINS=1 RUN_SLOW_TESTS=1 pytest tests/unit/test_main_boots_offline.py`
  rewrites `main_boots_offline_url_map.txt` from the booted app (header kept, sorted); without the variable nothing is written. The implementer commits the rewritten pin and the task reviewer reads its diff (no separate approval); a new route must also be refused or listed in `PUBLIC` of `tests/unit/web/test_every_route_refuses_anonymous.py`. Line forms:
  - `wiring_none_ledger.txt`: `<key>  # <reason> <evidence>`, the key a path from `app` (exact), `Class.attr` (None on every walked
    instance of the class) or `Class.attr*` (any instance may be None; refused on adapter, service, handler and composition classes);
  - `wiring_instance_ledger.txt`: `<module.Class> <count>  # <reason> <evidence>`; a count above 1 names where each instance is built.
- **Mutation** (`wire-drop` replaces one argument or dict value with `None`; `call-skip` drops one `attach_*`, `register_*` or `set_*`
  call). The weekly run covers the builder and `main.py` (registered targets). By hand and optional, one run per module, in the background
  (a full set of about 950 mutants takes about 2.5-3 hours; each mutant runs the graph tests in a child process):
  ```bash
  for m in src.composition.application_graph main src.composition.service_container src.composition.user_agent_factory \
           src.composition.slack_adapter_factory src.composition.telegram_adapter_factory; do
    python -m tests.support.semantic_mutants --module "$m" --operators wiring \
      --equivalent tests/unit/composition/wiring_equivalent_mutants.txt \
      --tests $(python -m tests.support.mutation_targets --tests-for "$m")
  done
  ```
  A survivor gets a new assertion in a new file; the reviewer alone rules a mutant equivalent in `wiring_equivalent_mutants.txt`.
- **Not covered, by decision:** a wrong but non-None object of the right type; producer and consumer payload fields that disagree;
  an enqueue literal in dead code; the rollback env switches; the wiring of `indexer_main`, `job_main`, `relay_main`; a secret that is
  empty in Secret Manager; the four env reads made at import time, before the seal. They are listed in the RFC's "Built (P9)" note.

## Mutation check: the weekly run

Mutation testing is the weekly cloud run, nothing else (owner decision 2026-10-27: mutant hunting during normal work only adds cost). Between
runs the code lives with its survivors; no package waits for a clean local pass. The driver below stays as a tool anyone may run by hand. Mutants: a dropped `order_by`/`where`/`select` field/`model_dump` field/dict key of a
written document/write/forwarded keyword, a flipped sort direction, an `except` that returns success.

```bash
python -m tests.support.semantic_mutants --module src.adapters.firestore_repo \
    --tests $(python -m tests.support.mutation_targets --tests-for src.adapters.firestore_repo) [--equivalent FILE]   # one child pytest per mutant; exit 1 if any survives
```

The test list is computed, not hand-written: `python -m tests.support.mutation_targets --tests-for <src.module>` prints every `tests/unit`
test file that reaches the module through imports (it follows imports only through `tests/` modules, never through `src/`). The six wiring targets also get the graph group `graph_tests()` (every unit test reaching `prod_graph`, `worker_triggers`, `main` or `application_graph`). `extra` is the escape hatch for a test that reaches the module only through `src` or only through a conftest fixture
(conftests are never followed). For a registered target `--tests-for <m>` prints the full list (computed closure + graph group + `extra`); it exits 2 with a message when no unit test reaches the module.

Statuses: killed, survived, equivalent, and `error` (the selected tests never loaded the module, or the run could not tell; exit 2;
not a pass). `except-success` skips handlers that only log and already return the success value. Slow tests run in the mutant
children (`RUN_SLOW_TESTS=1`).

A survivor means the tests do not notice that defect: write the assertion that does (the Monday routine does it from the weekly
report). Run that new test on the **unmutated** code first: it passes → `test-gap` (the code was right, a check was missing); it
fails → `code-bug` (a real defect in current code; the weekly routine has no `src/` authority, so it pins it as a strict xfail
`MUT-D<n>` and tells the owner). Each survivor's class goes into the owner's mutation ledger (kept outside this repository; the
`delivery` skill names it), so the mutants' yield in real defects is measurable. `--equivalent` takes `key  # reason` lines (the
8-hex key in the report) and is the reviewer's tool, never the implementer's.

Registering a package's modules in `tests/support/mutation_targets.py` (`MutationTarget(module, equivalent, operators="default", graph_group=False, extra=())` in `TARGETS`)
is part of "package done" (the run itself is not); a unit test fails when a `*_equivalent_mutants.txt` file has no registered target. The driver takes
`--report PATH` (JSON) and `--no-fail` (exit 0 whatever survived). The `Mutation survivors` workflow (`.github/workflows/mutation.yml`,
Sunday 02:00 UTC and `workflow_dispatch` with an optional `module`) runs one job per registered module with `--report --no-fail` and
uploads `mutation-<slug>` per module and `survivors` (`survivors.md`, `survivors.json`). `survivors.md` lists every registered module: `missing` when a job produced no report (baseline failed, timed out), `partial (N of M)` when the job was cut; the report is rewritten after each mutant. The verdict is also printed as check-run annotations (≤ 10 per step, 4000 chars each) for the cloud reader. Survivors are read by the Monday routine (a
scheduled cloud session), which writes the killing assertion or proposes an `equivalent` line, as a test-only PR. A local run is optional and
never a condition of "done".

## Clock tests (P16)

- Instants come from `tests.support.clock_instants` (the 2026 DST transitions, skipped and repeated local hours, month ends, UTC midnight); never
  retype one in a test.
- Pin a clock per module with `pin_datetime(monkeypatch, module, instant)`, and re-pin at every step that moves time. A third-party clock such as
  PyJWT's is pinned in its own module. No freezegun or time-machine.
- Compare a fold-1 value through `.astimezone(timezone.utc)`: aware datetimes with different folds can compare equal.
- Example zones are Europe/Berlin, Europe/Helsinki and America/New_York.
- A new test with a DST or month boundary uses this harness. Open defects are strict xfails with their `P16-D<n>` id
  (`TEST_COVERAGE_SYSTEMIC_RFC.md` -> "Built (P16)").

## Redelivery and interleaving (P15)

- **`tests/support/redelivery_world.py`** is the world for any handler-level redelivery test: the real `WorkerHandler`, claim store, note
  store, `UserNotificationService`, `RemindersService` and `GcpTaskQueue` on one `EvalDb`, a scripted agent turn and recording channels, plus
  `deliver`, `deliver_twice` (two task names the claim does not merge, in sequence or under `asyncio.gather`; `names=None` sends Scheduler
  bodies), `deliver_with_retry` (a retry is delivered only when Cloud Tasks would: a raise, `>= 500` or `429`) and `Rendezvous`. Use it; do not
  build a second one.
- **Inside a strict `xfail(raises=AssertionError)` every precondition uses `require(cond, what)` or `Twice.reraise()`** (they raise
  `PreconditionFailed`, which is not the expected failure), never a bare `assert`: a bare `assert` precondition would turn a broken setup into a
  silent XFAIL. Only the outcome assertion is a bare `assert`. A gathered pin takes `require(rv.met and not rv.stalled, ...)` as its race evidence.
  The exception: when the flipping fix keeps the losing delivery away from the race point (a lease, so it never reads), `rv.met` would flip with the
  fix, and the precondition is `twice.overlapped and rv.reached.is_set() and not rv.stalled` (P15-D5, first test of
  `test_worker_redelivery_chains.py`).
- **A gathered test forces its interleaving and asserts that it happened.** `collection.yield_on_get = True` interleaves document reads only; a
  query never yields, so a race whose read is a query takes a `Rendezvous` (or an `asyncio.Event` gate). A serial run must fail the assertion that
  the interleaving happened.
- **Agent instance state** (`tests/unit/agents/test_agent_request_state_meta.py`): agent instances are per-user singletons serving concurrent
  executions, so a write to shared state outside `__init__` (`self.*`, class state, module-level names) in any class under `src/` that derives from
  `BaseAgent` needs a line in `tests/unit/agents/agent_instance_writes_allowlist.txt` with a reason (`constructor-only`, `lazy-cache`,
  `cross-request-by-design`, `port-call`, `dead-write`, `defect:<id>`) and evidence; `constructor-only`, `lazy-cache` and the lock are checked
  mechanically, and a lock is never a reason alone: it comes only as `cross-request-by-design+lock-guarded` or `lazy-cache+lock-guarded`. A line is
  `<path>::<Class>.<method>::<key>  # <reason> <evidence, at least two words>` (the key is the dotted attribute path up to the first subscript or call,
  no line numbers). It fails both ways (a write with no line; a line whose write is gone).

## Service seams (P13)

- **`tests/support/seam_world.py`** is the world for a test that crosses a service seam from an entry path (`/worker`, `AgentWorkerHandler`,
  `ConversationHandler`): the production services between the handler and the agent (`AgentCoordinator`, `UserNotificationService`,
  `DocumentDeliveryService`, `TaskDispatchService`, `RemindersService`, `WorkerHandler`, the session store), wired as `build_application_graph`
  wires them (the deltas are in the module docstring), with scripted stand-in agents registered in the real coordinator, recording channels,
  media ports and Cloud Tasks client, all on one `EvalDb`. Use it; do not build a second one. Its self-test is `tests/unit/support/test_seam_world.py`.
- **Set the primary channel and make the origin differ from it**, so a dropped `origin_channel_id` / `origin_platform` lands the result
  somewhere else and the test sees it. Assert the outcome the boundary recorded (the channel and text posted, the history pair, the Cloud
  Task body), not a call.
- **A stand-in that runs out of steps is swallowed**: the coordinator turns the `RuntimeError` into a failed turn. Every pin, and every
  control whose pass could come from an exhausted script, starts with `require(not world.unscripted(), ...)`. Strict xfails follow the
  rules of "Redelivery and interleaving (P15)" (`require` for preconditions, the outcome assert last).
- **The PromptBuilder and SearchEnrichment seams** run through the real Smart and Router: `tests/unit/agents/core/test_smart_prompt_enrichment_seam.py`
  (real `PromptBuilder` over a seeded prompt store, `tests/support/prompt_seed.py`; the Smart is built with `tests/support/tool_e2e.smart_rig`).
- Files: `tests/unit/handlers/test_seam_worker_notices.py` (worker notices and video), `test_seam_agent_worker_delivery.py`,
  `test_seam_chat_turn.py`, `test_seam_background_runs.py`. Open defects are strict xfails with their `P13-D<n>` id
  (`TEST_COVERAGE_SYSTEMIC_RFC.md` -> "Built (P13)").

## Tool fields (P14)

- **`tests/unit/agents/tool_field_ledger.txt`** has one sorted line per field a model can send to a tool that writes something:
  `<surface>:<tool>.<path>  # <class> <evidence>`. `tests/support/tool_fields.py` `declared()` enumerates the fields from the production tool
  declarations; `tests/unit/agents/test_every_tool_field_is_stored_or_refused.py` fails both ways (a declared field with no line, a line for
  a field no longer declared) and checks every reference.
- **Classes:** `stored <ref>` (the referenced test asserts the stored effect), `refused <ref>`, `noted <ref>` (not used, and named in the tool
  result), `prompt-input <ref>` (reaches the model request and is stored nowhere), `verification <ref>` (checked, never stored),
  `read-only <reason>` (only a tool in `READ_ONLY` or a field in `READ_ONLY_FIELDS` of `tests/support/tool_fields.py`), `defect:P14-D<n> <ref>`
  (the declared field is itself the defect; its test is a strict xfail carrying that id). A `<ref>` is `<repo path>::<test name>`, lives in
  the surface's own e2e file and, except for `defect:`, is not an xfail; the field's leaf name must occur in the test's body or in a module-level
  constant or builder function it loads.
- **Add a field:** send it in a scripted call of the surface's e2e file (a module constant, or a module-level builder when ids exist only
  after seeding), assert its stored effect in a test there, then add the ledger line in sorted order.
- **The harness:** `tests/support/scripted_llm.py` scripts only the model; the agent's tool loop runs for real. A script that runs out raises
  `ScriptExhausted` (a `RuntimeError`, never an `AssertionError`, so a strict xfail does not swallow it) and counts in `unanswered`; **every test
  ends with the two script facts**, `llm.unanswered == 0 and llm.pending() == 0` (`require` them inside a strict xfail); each e2e file has one
  `test_the_scripted_calls_use_every_declared_field` (`unused(surface, tool, calls) == []` per tool), which a new field also fails. `tests/support/tool_e2e.smart_rig` builds a real Smart over a scripted
  model; P13 card F uses its keywords.
- Files: `test_consolidation_tool_fields_e2e.py`, `test_notes_agent_tool_fields_e2e.py`, `test_tasks_agent_tool_fields_e2e.py`,
  `test_file_management_drive_tool_fields_e2e.py` (`tests/unit/agents/`), `core/test_smart_local_tools_e2e.py`,
  `tests/unit/services/test_fact_review_tool_fields_e2e.py`. Open defects are strict xfails with their `P14-D<n>` id
  (`TEST_COVERAGE_SYSTEMIC_RFC.md` -> "Built (P14)").

## Markers and the machine lock (P1, `tests/plugins/`)

- **`@pytest.mark.slow`** — the test takes over 0.5 s with coverage on (setup + call + teardown). Skipped unless
  `RUN_SLOW_TESTS=1`; `make ci` sets it. Put it on the test, or `pytestmark` when the cost is a module- or class-scoped
  fixture (under xdist whichever test uses the fixture first on a worker pays, so mark all its users). Do not mark a test
  slow to get a green run before asking whether it can be fast.
- **Other markers.** `timing`: asserts on wall-clock or CPU time; skipped unless `RUN_TIMING_TESTS=1` (`make test-timing`,
  CI after a merge to main); never in `make check`. `docker`: needs the built sandbox image and a daemon; skipped unless
  `SANDBOX_TEST_IMAGE` is set. `requirement("REQ-XXX")`: ties a test to a business requirement. `performance`: manual
  benchmarks against real Firestore.
- **`make test-affected` selects by executed Python lines**, so it can miss a test whose input is a data file or code run
  in a child process; so an intermediate commit adds the tests of any data folder it touched, and `make check` before a push is mandatory (once per push, not per commit). It passes `-n`, so it takes the one-suite lock and waits behind another session's
  full run.
- **One full suite at a time** — a session with `-n`, or with 2000+ tests, takes a `flock` on
  `$TMPDIR/alekbot-pytest-suite.lock` (`tests/plugins/suite_lock.py`), prints the holder every 30 s while it waits and
  gives up after `PYTEST_LOCK_TIMEOUT_S` (1800). `PYTEST_SUITE_LOCK=0` disables it. Tests that run an inner pytest through
  `pytester` start from `tests/support/plugin_runs.py: isolate_switches`, so the outer run's switches and lock do not leak.
- Tests of the machinery: `tests/unit/test_pytest_plugins_*.py`.

## Existing tests are not edited to pass

The one who writes the code never edits an existing test to make it pass; a reviewer decides each edit
(root `CLAUDE.md` -> "Tests - REVIEWER RULE"). A failing test is the specification speaking. The network guard may fail
existing tests: record test id, exception and what it connects to, and hand the list to a reviewer.

Mechanical adaptations (R7): a rename or a signature or constructor-argument change that breaks existing tests goes in the implementer's
own commit titled `test(mechanical): ...` (an import path fixed after a module rename you performed needs no reviewer at all). The reviewer
reads that commit as a whole and checks one thing: no assertion changed. An assertion is changed when a different value, condition or count
is asserted; renaming an identifier or keyword inside an assert after a rename is mechanical. Behavioural edits stay per test under the reviewer rule.

Tests of deleted code are deleted in the same PR by the implementer and listed in the PR body; the reviewer checks that each deleted
test's subject is gone (not a weakening).
