# Tests

pytest + pytest-asyncio. asyncio_mode=auto.

## Running

```bash
make test              # All
make test-unit         # tests/unit/
make test-integration  # tests/integration/
make test-e2e-all      # E2E all agents (against real APIs)
```

## Structure

```
tests/
  conftest.py      — Shared fixtures: mock_env_config, mock_llm_port, mock_repository
  unit/            — No external dependencies. AsyncMock for ports.
  integration/     — With mocked external services.
  performance/     — Benchmarks (@pytest.mark.performance).
```

## Test Pattern

```python
class TestMyService:
    @pytest.fixture
    def service(self, mock_repository):
        return MyService(repository=mock_repository)

    @pytest.mark.asyncio
    async def test_success(self, service, mock_repository):
        mock_repository.add_fact = AsyncMock(return_value="id123")
        result = await service.process(entity)
        assert result == "id123"
        mock_repository.add_fact.assert_called_once()
```

## Rules

- Port mocks: `AsyncMock(spec=PortClass)`.
- Marker `@pytest.mark.requirement("REQ-XXX")` for business requirements.
- One test class per service/agent.
- Arrange-Act-Assert.

## Markers, budget and the machine lock (P1, `tests/plugins/`)

- **`@pytest.mark.slow`** — the test takes over 0.5 s with coverage on (setup + call + teardown; half the 1 s CI budget, so CI does not flake). Skipped unless
  `RUN_SLOW_TESTS=1`; `make ci` sets it. Put it on the test, or `pytestmark` when the cost is a module- or class-scoped
  fixture (under xdist whichever test uses the fixture first on a worker pays, so mark all its users). Do not mark a test
  slow to get a green run before asking whether it can be fast.
- **Per-test budget** — the PR job sets `PYTEST_DURATION_BUDGET_S=1.0`; any unmarked test over it fails the run
  (`tests/plugins/duration_budget.py`). `slow` and `timing` tests are exempt. Locally it is off; `make check` prints
  `--durations=25`.
- **`@pytest.mark.datafile`** — the test's input is a file (a prompt snapshot, an RFC, `config/firestore.indexes.json`, a
  cloudbuild yaml, a skill, a fixture, a `.py` read as text or via `inspect.getsource`) or its code runs in a child
  process. testmon cannot select such a test, so `make test-affected` always runs the marked set. Use `pytestmark` when
  most tests of a module need it.
- **The audit keeps the marker honest** — `PYTEST_DATAFILE_AUDIT=1` (set by `make ci`, usable by hand) records which
  tests open a file under the repo or spawn a process, and fails the run for the unmarked ones, printing the files.
  Reads made when a test module is collected (module-level code, imports of modules that read data) count for every test
  of that module; import-machinery reads, pytest's own reads, source lines read to format a traceback or a warning, and
  lazy imports inside a test do not. Marked tests that touched nothing are listed as stale (informational).
- **What the audit cannot see:** directory listings (`glob`, `iterdir`), `exists()`/`stat()` checks, and reads made by stray
  background threads. A test whose input is such a thing needs `datafile` by hand.
- **One full suite at a time** — a session with `-n`, or with 2000+ tests, takes a `flock` on
  `$TMPDIR/alekbot-pytest-suite.lock` (`tests/plugins/suite_lock.py`), prints the holder every 30 s while it waits and
  gives up after `PYTEST_LOCK_TIMEOUT_S` (1800). `PYTEST_SUITE_LOCK=0` disables it. Tests that run an inner pytest through
  `pytester` start from `tests/support/plugin_runs.py: isolate_switches`, so the outer run's switches and lock do not leak.
- Tests of the machinery: `tests/unit/test_pytest_plugins_*.py`.
