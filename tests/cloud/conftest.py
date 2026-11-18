"""Fixtures of the cloud gate (TEST_COVERAGE_SYSTEMIC_RFC §4): real writes to the Firestore TEST database, behind the guards.

Settings (`TEST_FIRESTORE_DATABASE`, `TEST_SA_EMAIL`, `GOOGLE_CLOUD_PROJECT`; the live `FIRESTORE_DATABASE` for the
comparison) are read from the environment or the repository's dotenv file inside this process, and never printed: a
guard that fails says which rule broke, not the values. Without `RUN_CLOUD_TESTS=1` (set by `make test-cloud`), or with any setting missing, every test here skips.

Guards, before any write: no emulator in the environment; the database name ends in `-test` and is not the live
database; the credentials are those of the test service account (impersonated from the default credentials, or, under Workload
Identity Federation in CI, the default credentials themselves; the email is checked on both paths), which holds datastore.user and
datastore.indexAdmin on the test database only); the constructed client's database and project are the configured ones.

The store kits run here too (`test_kits_cloud.py`): the bottom of this file collects each kit file of `KITS` and overrides
`kit_db` with the real backend of `tests/support/kit_backend.py`, behind the same guards.
"""
import ast
import importlib
import os
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Set

import google.auth
import pytest
from dotenv import dotenv_values
from google.api_core.exceptions import AlreadyExists
from google.auth import impersonated_credentials
from google.cloud import firestore, firestore_admin_v1

from src.config.environment import EnvironmentConfig
from tests.support.cloud_credentials import select_credentials
from tests.support.cloud_indexes import admin_index, existing_signature, of_group, wanted_signature
from tests.support.firestore_eval import EvalDb
from tests.support.kit_backend import RealKitBackend
from tests.support.store_inventory import KITS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
INDEX_READY_TIMEOUT_S = 15 * 60
INDEX_POLL_S = 10
Index = firestore_admin_v1.Index


@lru_cache(maxsize=None)
def _dotenv() -> Dict[str, Any]:
    return dotenv_values(REPO_ROOT / ".env")


def _setting(name: str) -> str:
    return os.environ.get(name) or _dotenv().get(name) or ""


@pytest.fixture(scope="module")
def cloud() -> SimpleNamespace:
    if os.environ.get("RUN_CLOUD_TESTS") != "1":
        pytest.skip("cloud gate: run with `make test-cloud` (RUN_CLOUD_TESTS=1); a bare pytest never writes to the test database")
    database, sa, project = _setting("TEST_FIRESTORE_DATABASE"), _setting("TEST_SA_EMAIL"), _setting("GOOGLE_CLOUD_PROJECT")
    if not (database and sa and project):
        pytest.skip("cloud gate not configured: TEST_FIRESTORE_DATABASE, TEST_SA_EMAIL and GOOGLE_CLOUD_PROJECT must all be set")
    live = _setting("FIRESTORE_DATABASE") or EnvironmentConfig().firestore_database_id
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.fail("guard: FIRESTORE_EMULATOR_HOST is set, so a client would never reach the test database")
    if not database.endswith("-test"):
        pytest.fail("guard: the test database name does not end in -test")
    if database == live:
        pytest.fail("guard: the test database is the live database")
    source, _ = google.auth.default(scopes=SCOPES)
    try:
        credentials = select_credentials(source, sa, lambda src: impersonated_credentials.Credentials(
            source_credentials=src, target_principal=sa, target_scopes=SCOPES, lifetime=3600))
    except PermissionError:
        pytest.fail("guard: the credentials are not those of the test service account")
    return SimpleNamespace(database=database, project=project, credentials=credentials)


@pytest.fixture
async def client(cloud: SimpleNamespace):  # type: ignore[no-untyped-def]
    db = firestore.AsyncClient(project=cloud.project, database=cloud.database, credentials=cloud.credentials)
    if db._database != cloud.database or db.project != cloud.project:
        pytest.fail("guard: the constructed client does not point at the test database and project")
    yield db
    db.close()      # synchronous in google-cloud-firestore 2.34


# ------------------------------------------------------------------------------------------------ indexes

@pytest.fixture(scope="module")
def ensure_indexes(cloud: SimpleNamespace) -> Callable[[str, List[Dict[str, Any]]], None]:
    """`ensure(collection, indexes)`: create the missing indexes (in `config/firestore.indexes.json` shape) of a collection
    on the test database and wait until all are READY. Created once and kept: the collection ids are fixed names."""
    admin = firestore_admin_v1.FirestoreAdminClient(credentials=cloud.credentials)
    done: Set[str] = set()
    failed: Set[str] = set()

    def ensure(collection: str, indexes: List[Dict[str, Any]]) -> None:
        if collection in done:
            return
        if collection in failed:       # every later test of the group fails at once, not after another 15-minute wait
            pytest.fail(f"the indexes of {collection} already failed in this module")
        try:
            _ensure(collection, indexes)
        except BaseException:
            failed.add(collection)
            raise
        done.add(collection)

    def _ensure(collection: str, indexes: List[Dict[str, Any]]) -> None:
        parent = f"projects/{cloud.project}/databases/{cloud.database}/collectionGroups/{collection}"
        have = {existing_signature(i) for i in of_group(admin.list_indexes(parent=parent), collection)}
        for index in indexes:
            if wanted_signature(index) not in have:
                try:
                    admin.create_index(parent=parent, index=admin_index(index))
                except AlreadyExists:
                    pass
        deadline = time.monotonic() + INDEX_READY_TIMEOUT_S
        while True:
            states = {existing_signature(i): i.state for i in of_group(admin.list_indexes(parent=parent), collection)}
            wanted = [states.get(wanted_signature(index)) for index in indexes]
            if Index.State.NEEDS_REPAIR in wanted:
                pytest.fail(f"an index of {collection} needs repair")
            if all(state == Index.State.READY for state in wanted):
                return
            if time.monotonic() > deadline:
                pytest.fail(f"the indexes of {collection} were not READY within {INDEX_READY_TIMEOUT_S // 60} minutes")
            time.sleep(INDEX_POLL_S)

    return ensure


# ------------------------------------------------------------------------------------------------ store kits (P12)
# `test_kits_cloud.py` is collected by `KitSuite`: one `KitModule` per kit file of `KITS`, so the kit runs here from its own
# file with this directory's fixtures (the guards above and the real `kit_db`); never the unit conftest's network guard.

KITS_SUITE = "test_kits_cloud.py"


def _kit_files() -> List[str]:
    return sorted({path for files in KITS.values() for path in files})


def _on_the_seam(path: Path) -> bool:
    """The kit takes `kit_db` (a parameter of some function or fixture): it is converted to the backend seam."""
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and any(arg.arg == "kit_db" for arg in node.args.args + node.args.kwonlyargs)
               for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))


class KitModule(pytest.Module):
    """One kit file, imported under its dotted name and collected as a child of the suite file."""

    def __init__(self, *, kit_file: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.kit_file = kit_file

    def _getobj(self) -> Any:
        _refuse_fakes(self.config)
        module = importlib.import_module(self.kit_file[:-len(".py")].replace("/", "."))
        built = sorted(name for name, value in vars(module).items() if isinstance(value, _fake_classes()))
        if built:
            raise self.CollectError(f"{self.kit_file} holds an evaluating fake at module level ({', '.join(built)}): "
                                    "build it per test over kit_db")
        return module

    def collect(self) -> Any:
        if not _on_the_seam(REPO_ROOT / self.kit_file):
            pytest.skip(f"{self.kit_file} does not take kit_db yet (not converted to the real backend)")
        return super().collect()


class KitSuite(pytest.File):
    def collect(self) -> Iterator[KitModule]:
        if os.environ.get("RUN_CLOUD_TESTS") != "1":
            pytest.skip("cloud gate: run with `make test-cloud` (RUN_CLOUD_TESTS=1)")
        for kit_file in _kit_files():
            stem = Path(kit_file).stem
            yield KitModule.from_parent(self, path=self.path, name=stem, nodeid=f"{self.nodeid}::{stem}", kit_file=kit_file)


def pytest_pycollect_makemodule(module_path: Path, parent: Any) -> Any:
    if module_path.name == KITS_SUITE:
        return KitSuite.from_parent(parent, path=module_path)
    return None


def pytest_collection_modifyitems(config: Any, items: List[Any]) -> None:
    """A kit test marked `h1_only` drives a hook only the fake has: skipped on the real backend."""
    for item in items:
        mark = item.get_closest_marker("h1_only")
        if mark is not None and item.getparent(KitModule) is not None:
            item.add_marker(pytest.mark.skip(reason=f"h1_only: {mark.args[0] if mark.args else 'fake-only hook'}"))


def _fake_classes() -> tuple:
    from tests.unit.adapters.firestore_drive_eval_fakes import EvalDb as DriveEvalDb
    return (EvalDb, DriveEvalDb)


def _refuse_fakes(config: Any) -> None:
    """From the first kit import to the end of the session, building an evaluating fake fails: at import time, in a
    module- or session-scoped fixture and in a test alike. A kit test here that ran on the fake would prove nothing."""
    if getattr(config, "_kit_fakes_refused", False):
        return
    config._kit_fakes_refused = True

    def refuse(*_a: Any, **_k: Any) -> None:
        pytest.fail("a kit builds an evaluating fake in the cloud run: build the store over kit_db, "
                    "or mark the test h1_only with the reason")

    def restore(fake: Any, original: Any) -> None:
        if original is None:
            del fake.__init__
        else:
            fake.__init__ = original

    for fake in _fake_classes():
        original = fake.__dict__.get("__init__")
        fake.__init__ = refuse
        config.add_cleanup(lambda fake=fake, original=original: restore(fake, original))


@pytest.fixture(autouse=True)
def _cloud_kits_reach_only_the_test_database(request: Any, monkeypatch: Any) -> None:
    """The unit network guard does not cover a kit here; instead every Firestore client a kit test constructs must point at
    the test database and project, or the test fails before the client is used."""
    if request.node.getparent(KitModule) is None:
        return
    cloud = request.getfixturevalue("cloud")
    from google.cloud.firestore_v1.async_client import AsyncClient
    from google.cloud.firestore_v1.client import Client

    for cls in (Client, AsyncClient):
        original = cls.__init__

        def checked(self: Any, *a: Any, _original: Any = original, **k: Any) -> None:
            _original(self, *a, **k)
            if self._database != cloud.database or self.project != cloud.project:
                pytest.fail("guard: a kit test constructed a Firestore client that is not on the test database and project")

        monkeypatch.setattr(cls, "__init__", checked)


@pytest.fixture
def kit_db(cloud: SimpleNamespace, client: Any, ensure_indexes: Callable[[str, List[Dict[str, Any]]], None]) -> Any:
    """The kits' backend on the test database (tests/support/kit_backend.py): the guarded async client, a sync client checked
    the same way, the test's collections purged before first use and after the test."""
    sync = firestore.Client(project=cloud.project, database=cloud.database, credentials=cloud.credentials)
    if sync._database != cloud.database or sync.project != cloud.project:
        pytest.fail("guard: the constructed sync client does not point at the test database and project")
    backend = RealKitBackend(client, sync, ensure_indexes)
    yield backend
    try:
        backend.purge_touched()
    finally:
        sync.close()
