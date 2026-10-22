# Tests

pytest + pytest-asyncio, `asyncio_mode=auto`. Why these rules: `docs/10_rfcs/TEST_COVERAGE_SYSTEMIC_RFC.md`.

## Running

```bash
make check             # lint + mypy + unit suite (no slow tests, no coverage) + offline G2: the pre-commit gate
make test-affected     # inner loop: only the unit tests testmon selects for your changes
make ci                # the PR job: check + slow tests, coverage gates (one full run)
make test-integration  # tests/integration/ (some talk to real services); make test-e2e-all: real APIs
```

```
tests/
  conftest.py     — fixtures + the plugin in tests/plugins/ (suite lock)
  plugins/        — pytest plugins: the suite lock, `network_guard.py` (the offline G2 run)
  unit/           — offline, no real service. Fakes and in-memory repositories. conftest.py = the network guard
  integration/    — mocked or scripted services; the offline G2 end-to-end test runs in `make check`
  support/        — shared fakes (fact_store.py, firestore_eval.py), network_guard.py, plugin_runs.py, semantic_mutants/
  contracts/      — adapter contract rules (adapter_contracts.py)
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
- **No test calls a real LLM**, and no unit test touches the network (below). A test that needs the real service is an
  integration test and lives in `tests/integration/`.
- One test class per service or agent. `@pytest.mark.requirement("REQ-XXX")` for business requirements.
  Arrange-Act-Assert.

```python
async def test_update_content_reembeds_and_bumps_version(store):   # store = FactStore(): real adapter, repo and service on a fake Firestore
    fid = await _create(store)
    res = await store.adapter.update_fact(fid, {"content": "lives in Springfield, Ohio"}, ACC)
    doc = store.docs[fid]                                           # assert on the raw stored document, not on a mock call
    assert doc["text"] == "mother is Mariya Doe" and doc["version"] == 2
```

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

## Coverage floors and the untested-module list

- `.coveragerc` has `branch = True` and measures `src/` plus the root `*_main.py`; `make ci` fails under the global
  `--cov-fail-under`, the per-file table (`scripts/check_coverage_thresholds.py`) and the **per-layer floors**
  (`scripts/check_coverage_layers.py`, branch-aware percent per layer). Floors only rise: raise a floor only to min(local `make ci`, the CI 'Coverage per layer' notice) − 0.3. The script
  prints "floor can rise to X" when that would be 0.5 above the floor; raise it in the same change. Never lower one without the owner's per-layer OK.
- `tests/unit/test_every_src_module_is_imported_by_a_test.py`: every module under `src/` and every root `*_main.py` is
  imported by some test, or listed with a reason in `tests/unit/untested_modules_allowlist.txt`. A new module needs a
  test, not an allowlist line. The list only shrinks: the test fails when a listed module is now imported. The check is `slow`,
  so it fails only in `make ci`.

## Mutation check: the done-criterion for a package

Every package ends with this check: it is done when every mutant of its modules is killed by its tests or
ruled equivalent by the reviewer. Mutants: a dropped `order_by`/`where`/`select` field/`model_dump` field/dict key of a
written document/write/forwarded keyword, a flipped sort direction, an `except` that returns success.

```bash
python -m tests.support.semantic_mutants --module src.adapters.firestore_repo \
    --tests tests/unit/adapters/test_fact_store_roundtrip.py [--equivalent FILE]   # one child pytest per mutant; exit 1 if any survives
```

Statuses: killed, survived, equivalent, and `error` (the selected tests never loaded the module, or the run could not tell; exit 2;
not a pass). `except-success` skips handlers that only log and already return the success value. Slow tests run in the mutant
children (`RUN_SLOW_TESTS=1`).

A survivor means the tests do not notice that defect: write the assertion that does. `--equivalent` takes
`key  # reason` lines (the 8-hex key in the report) and is the reviewer's tool, never the implementer's.

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
  in a child process; `make check` before a commit is mandatory. It passes `-n`, so it takes the one-suite lock and waits behind another session's
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
