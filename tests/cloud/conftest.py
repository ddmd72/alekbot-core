"""Fixtures of the cloud gate (TEST_COVERAGE_SYSTEMIC_RFC §4): real writes to the Firestore TEST database, behind the guards.

Settings (`TEST_FIRESTORE_DATABASE`, `TEST_SA_EMAIL`, `GOOGLE_CLOUD_PROJECT`; the live `FIRESTORE_DATABASE` for the
comparison) are read from the environment or the repository's dotenv file inside this process, and never printed: a
guard that fails says which rule broke, not the values. Without `RUN_CLOUD_TESTS=1` (set by `make test-cloud`), or with any setting missing, every test here skips.

Guards, before any write: no emulator in the environment; the database name ends in `-test` and is not the live
database; the credentials are impersonated credentials of the test service account (which holds datastore.user on the
test database only); the constructed client's database and project are the configured ones.
"""
import os
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Set

import google.auth
import pytest
from dotenv import dotenv_values
from google.api_core.exceptions import AlreadyExists
from google.auth import impersonated_credentials
from google.cloud import firestore, firestore_admin_v1

from src.config.environment import EnvironmentConfig

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
    credentials = impersonated_credentials.Credentials(
        source_credentials=source, target_principal=sa, target_scopes=SCOPES, lifetime=3600)
    if credentials.service_account_email != sa:
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

def _wanted_signature(index: Dict[str, Any]) -> tuple:
    return tuple((f["fieldPath"], "VECTOR" if "vectorConfig" in f else "ASCENDING") for f in index["fields"])


def _existing_signature(index: Any) -> tuple:
    return tuple((f.field_path, "VECTOR" if f.vector_config.dimension else f.order.name)
                 for f in index.fields if f.field_path != "__name__")     # the server appends __name__ to a composite


def _to_admin_index(index: Dict[str, Any]) -> Any:
    vector = Index.IndexField.VectorConfig
    fields = [Index.IndexField(field_path=f["fieldPath"], vector_config=vector(dimension=f["vectorConfig"]["dimension"],
                                                                                 flat=vector.FlatIndex()))
              if "vectorConfig" in f else
              Index.IndexField(field_path=f["fieldPath"], order=Index.IndexField.Order.ASCENDING)
              for f in index["fields"]]
    return Index(query_scope=Index.QueryScope.COLLECTION, fields=fields)


@pytest.fixture(scope="module")
def ensure_indexes(cloud: SimpleNamespace) -> Callable[[str, List[Dict[str, Any]]], None]:
    """`ensure(collection, indexes)`: create the missing indexes (in `config/firestore.indexes.json` shape) of a collection
    on the test database and wait until all are READY. Created once and kept: the collection ids are fixed names."""
    admin = firestore_admin_v1.FirestoreAdminClient(credentials=cloud.credentials)
    done: Set[str] = set()

    def ensure(collection: str, indexes: List[Dict[str, Any]]) -> None:
        if collection in done:
            return
        parent = f"projects/{cloud.project}/databases/{cloud.database}/collectionGroups/{collection}"
        have = {_existing_signature(i) for i in admin.list_indexes(parent=parent)}
        for index in indexes:
            if _wanted_signature(index) not in have:
                try:
                    admin.create_index(parent=parent, index=_to_admin_index(index))
                except AlreadyExists:
                    pass
        deadline = time.monotonic() + INDEX_READY_TIMEOUT_S
        while True:
            states = {_existing_signature(i): i.state for i in admin.list_indexes(parent=parent)}
            wanted = [states.get(_wanted_signature(index)) for index in indexes]
            if Index.State.NEEDS_REPAIR in wanted:
                pytest.fail(f"an index of {collection} needs repair")
            if all(state == Index.State.READY for state in wanted):
                done.add(collection)
                return
            if time.monotonic() > deadline:
                pytest.fail(f"the indexes of {collection} were not READY within {INDEX_READY_TIMEOUT_S // 60} minutes")
            time.sleep(INDEX_POLL_S)

    return ensure
