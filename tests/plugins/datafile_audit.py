"""Data-file audit: finds the tests that `pytest --testmon` cannot select (TEST_COVERAGE_SYSTEMIC_RFC §4b).

testmon selects by executed Python lines. A test whose input is a file (a prompt snapshot, an RFC, a cloudbuild yaml,
a skill, a fixture, or a `.py` read as text) or whose code runs in a child process is invisible to it. Such tests carry
`@pytest.mark.datafile`; `make test-affected` always runs them.

With env `PYTEST_DATAFILE_AUDIT=1` this plugin attributes, per test phase (setup / call / teardown):
  - every `open` of a file under the rootdir that is not made by the import machinery or by pytest itself, and
  - every process spawn (`subprocess.Popen`, `os.posix_spawn`, `os.exec*`, `os.fork`, `os.system`, `os.spawn*`),
and at the end of the run fails when such a test lacks the `datafile` marker. Reads made while a test module is
collected (module-level code, imports of data-reading modules) belong to every test of that module. Marks that
touched nothing are listed as stale (informational): per test for a test's own mark, per module ("0 of N tests")
for a module-level mark. Unset: this plugin does nothing.

Mechanics: one `sys.addaudithook` per process (it cannot be removed), a fast path for events and paths it ignores, a
stack walk only for rootdir paths. Findings travel from xdist workers to the controller in `report.user_properties`.
A read is attributed to the nearest caller that is neither the stdlib nor a pytest-family package; a read made while a
traceback or a warning is formatted (linecache), by the import machinery, or inside a lazy import during a test is
not a data dependency of the test and is ignored.
"""

import json
import os
import sys
import sysconfig
import tempfile
from typing import Dict, List, Optional, Tuple

import pytest

ENV_AUDIT = "PYTEST_DATAFILE_AUDIT"
PROP = "datafile_audit"
# "own" (decorator on the test function itself) | "inherited" (class decorator or module pytestmark) | ""
MARK_PROP = "datafile_audit_mark"
MARKER = "datafile"
MAX_ENTRIES = 40  # per owner; the report prints a few, the set is only for "is it empty"

_SPAWN_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.system",
        "os.spawn",
    }
)
_IGNORED_DIRS = frozenset(
    {
        "__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        # a virtualenv or tox env inside the checkout is not repo data
        ".venv", "venv", "site-packages", "dist-packages", ".tox", ".nox",
    }
)
_IGNORED_NAME_PREFIXES = (".testmondata", ".coverage")
_PYTEST_FAMILY = frozenset(
    {
        "_pytest",
        "pytest",
        "pluggy",
        "xdist",
        "execnet",
        "pytest_cov",
        "coverage",
        "testmon",
        "pytest_asyncio",
        "pytest_mock",
    }
)
_LAZY_IMPORT_FUNCS = frozenset({"_find_and_load", "_find_and_load_unlocked", "_load_unlocked"})
_STDLIB_PREFIXES = tuple(
    {os.path.join(p, "")[:] for p in (sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib")) if p}
)

_kind_cache: Dict[str, str] = {}


def _kind(filename: str) -> str:
    """import | plumbing | trace | pytest | third | user."""
    kind = _kind_cache.get(filename)
    if kind is not None:
        return kind
    if filename.startswith("<frozen importlib"):
        kind = "import"
    elif filename.startswith("<frozen "):
        kind = "plumbing"
    elif "site-packages" in filename or "dist-packages" in filename:
        tail = filename.split("site-packages" + os.sep)[-1].split("dist-packages" + os.sep)[-1]
        package = tail.split(os.sep)[0]
        if package.endswith(".py"):
            package = package[:-3]
        kind = "pytest" if package in _PYTEST_FAMILY else "third"
    elif filename.startswith(_STDLIB_PREFIXES):
        kind = "trace" if os.path.basename(filename) in ("traceback.py", "warnings.py") else "plumbing"
    else:
        kind = "user"
    _kind_cache[filename] = kind
    return kind


class _Findings:
    __slots__ = ("files", "procs")

    def __init__(self) -> None:
        self.files: Dict[str, None] = {}
        self.procs: Dict[str, None] = {}

    def add_file(self, rel: str) -> None:
        if len(self.files) < MAX_ENTRIES:
            self.files[rel] = None

    def add_proc(self, what: str) -> None:
        if len(self.procs) < MAX_ENTRIES:
            self.procs[what] = None

    def update(self, other: "_Findings") -> None:
        for rel in other.files:
            self.add_file(rel)
        for what in other.procs:
            self.add_proc(what)

    def __bool__(self) -> bool:
        return bool(self.files or self.procs)

    def dump(self) -> str:
        return json.dumps({"files": list(self.files), "procs": list(self.procs)})

    @classmethod
    def load(cls, text: str) -> "_Findings":
        data = json.loads(text)
        found = cls()
        for rel in data.get("files", []):
            found.add_file(rel)
        for what in data.get("procs", []):
            found.add_proc(what)
        return found


class Recorder:
    """Per-session state and the audit callback. At most one is active in a process (a pytester run inside a
    session replaces the outer one for its duration and restores it)."""

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)
        self.root_prefix = self.root + os.sep
        temp = os.path.realpath(tempfile.gettempdir()) + os.sep
        # A rootdir inside the temp dir (a pytester run) must not exclude itself.
        self.temp_prefix: Optional[str] = None if self.root_prefix.startswith(temp) else temp
        self.window: Optional[str] = None  # "collect" | "test" | None
        self.current = _Findings()
        self.collected: Dict[str, _Findings] = {}
        self._busy = False

    # -- audit callback -------------------------------------------------------------------------------------------

    def on_event(self, event: str, args: tuple, caller) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            if event == "open":
                self._on_open(args[0], caller)
            else:
                self._on_spawn(event, args, caller)
        finally:
            self._busy = False

    def _on_open(self, path: object, caller) -> None:
        if isinstance(path, int):
            return
        if isinstance(path, os.PathLike):
            path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if not isinstance(path, str):
            return
        if path[:1] != os.sep:
            path = os.path.join(os.getcwd(), path)
        if not path.startswith(self.root_prefix):
            return
        if ".." in path or "//" in path:
            path = os.path.normpath(path)
            if not path.startswith(self.root_prefix):
                return
        rel = path[len(self.root_prefix) :]
        if rel.startswith("<") and rel.endswith(">"):  # a pseudo file name ("<unknown>", "<string>") joined to the cwd
            return
        if rel.startswith(_IGNORED_NAME_PREFIXES) or not _IGNORED_DIRS.isdisjoint(rel.split(os.sep)):
            return
        if self.temp_prefix is not None and path.startswith(self.temp_prefix):
            return
        if self._attributable(caller):
            self.current.add_file(rel)

    def _on_spawn(self, event: str, args: tuple, caller) -> None:
        if not self.window or not self._attributable(caller, spawn=True):
            return
        target = args[0] if args else ""
        if event == "subprocess.Popen" and len(args) > 1 and not target:
            target = args[1][0] if args[1] else ""
        try:
            label = os.fsdecode(target) if not isinstance(target, str) else target
        except TypeError:
            label = str(target)
        self.current.add_proc(f"{os.path.basename(label) or event}")

    def _attributable(self, frame, spawn: bool = False) -> bool:
        """True when the nearest caller that is not stdlib plumbing is test or library code reading data on its own."""
        formatting_a_trace = False
        owner_seen = False
        while frame is not None:
            kind = _kind(frame.f_code.co_filename)
            if not owner_seen:
                if kind == "plumbing":
                    pass
                elif kind == "trace":
                    formatting_a_trace = True
                elif kind == "import" or (kind == "pytest" and not spawn):
                    return False  # a child started by pytester or xdist inside a test is the test's own child
                else:
                    if formatting_a_trace:
                        return False
                    owner_seen = True
            elif self.window == "test" and kind == "import" and frame.f_code.co_name in _LAZY_IMPORT_FUNCS:
                return False
            frame = frame.f_back
        return owner_seen

    # -- phases ---------------------------------------------------------------------------------------------------

    def begin(self, window: str) -> None:
        self.current = _Findings()
        self.window = window

    def end(self) -> _Findings:
        self.window = None
        done, self.current = self.current, _Findings()
        return done


_active: Optional[Recorder] = None
_hook_installed = False


def _audit_hook(event: str, args: tuple) -> None:
    recorder = _active
    if recorder is None or recorder.window is None:
        return
    if event == "open" or event in _SPAWN_EVENTS:
        recorder.on_event(event, args, sys._getframe(1))


def _install_hook() -> None:
    global _hook_installed
    if not _hook_installed:
        sys.addaudithook(_audit_hook)
        _hook_installed = True


def _mark_kind(item: pytest.Item) -> str:
    """Decided where the item lives (a worker, under xdist): `keywords` also hold node names and parametrize ids."""
    if item.get_closest_marker(MARKER) is None:
        return ""
    return "own" if any(mark.name == MARKER for mark in item.own_markers) else "inherited"  # own_markers: the function's


class DataFileAudit:
    def __init__(self, root: str, is_worker: bool) -> None:
        self.recorder = Recorder(root)
        self.is_worker = is_worker
        self._previous: Optional[Recorder] = None
        # controller side
        self._found: Dict[str, _Findings] = {}
        self._marked: set = set()
        self._own_mark: set = set()
        self._skipped: set = set()
        self._seen: List[str] = []
        self.flagged: List[Tuple[str, _Findings]] = []
        self.stale: List[str] = []  # own-marked tests that touched nothing
        self.stale_modules: List[Tuple[str, int]] = []  # module-marked modules where no test touched data: (module, n)

    # -- both sides: recording -----------------------------------------------------------------------------------

    def pytest_configure(self, config: pytest.Config) -> None:
        global _active
        self._previous = _active
        _active = self.recorder
        _install_hook()

    def pytest_unconfigure(self, config: pytest.Config) -> None:
        global _active
        _active = self._previous

    @pytest.hookimpl(hookwrapper=True)
    def pytest_make_collect_report(self, collector):
        if not isinstance(collector, pytest.Module):
            yield
            return
        self.recorder.begin("collect")
        outcome = yield
        found = self.recorder.end()
        if found:
            self.recorder.collected.setdefault(str(collector.path), _Findings()).update(found)
        del outcome

    def _phase(self, item: pytest.Item, when: str):
        if when == "setup":
            item.user_properties.append((MARK_PROP, _mark_kind(item)))
        self.recorder.begin("test")
        yield
        found = self.recorder.end()
        if when == "setup":
            module_reads = self.recorder.collected.get(str(getattr(item, "path", "")))
            if module_reads:
                found.update(module_reads)
        if found:
            item.user_properties.append((PROP, found.dump()))

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        yield from self._phase(item, "setup")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        yield from self._phase(item, "call")

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item):
        yield from self._phase(item, "teardown")

    # -- controller: aggregation ----------------------------------------------------------------------------------

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if self.is_worker:
            return
        node = report.nodeid
        if node not in self._found:
            self._found[node] = _Findings()
            self._seen.append(node)
        if report.skipped:
            self._skipped.add(node)
        for key, value in report.user_properties:
            if key == PROP:
                self._found[node].update(_Findings.load(value))
            elif key == MARK_PROP and value:
                self._marked.add(node)
                if value == "own":
                    self._own_mark.add(node)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.is_worker:
            return
        for node in self._seen:
            if node in self._skipped:
                continue
            found = self._found[node]
            if found and node not in self._marked:
                self.flagged.append((node, found))
            elif not found and node in self._own_mark:
                self.stale.append(node)
        by_module: Dict[str, List[bool]] = {}
        for node in self._seen:
            if node in self._marked and node not in self._own_mark and node not in self._skipped:
                by_module.setdefault(node.split("::")[0], []).append(bool(self._found[node]))
        self.stale_modules = [(m, len(v)) for m, v in by_module.items() if not any(v)]
        if self.flagged and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.is_worker:
            return
        if self.flagged:
            terminalreporter.section(
                f"tests that read repo data or spawned a process without @pytest.mark.datafile ({len(self.flagged)})",
                red=True,
            )
            for node, found in self.flagged:
                terminalreporter.write_line(node)
                if found.files:
                    shown = list(found.files)[:3]
                    more = f" (+{len(found.files) - 3} more)" if len(found.files) > 3 else ""
                    terminalreporter.write_line(f"    reads:  {', '.join(shown)}{more}")
                if found.procs:
                    shown = list(found.procs)[:3]
                    more = f" (+{len(found.procs) - 3} more)" if len(found.procs) > 3 else ""
                    terminalreporter.write_line(f"    spawns: {', '.join(shown)}{more}")
            terminalreporter.write_line(
                "testmon cannot select these: add @pytest.mark.datafile (module-level `pytestmark` when most tests "
                "of the module need it) -- tests/CLAUDE.md."
            )
        if self.stale or self.stale_modules:
            terminalreporter.section(
                f"datafile marks that touched no repo file and spawned nothing "
                f"({len(self.stale)} tests, {len(self.stale_modules)} modules)"
            )
            for module, count in self.stale_modules:
                terminalreporter.write_line(f"module {module}: 0 of {count} marked tests touched data")
            for node in self.stale:
                terminalreporter.write_line(node)
            terminalreporter.write_line(
                "Informational: the marker may be stale and the set can shrink. A module-level mark is listed only "
                "when none of its tests touched anything."
            )


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get(ENV_AUDIT) != "1":
        return
    config.pluginmanager.register(
        DataFileAudit(str(config.rootpath), is_worker=hasattr(config, "workerinput")), "datafile-audit"
    )
