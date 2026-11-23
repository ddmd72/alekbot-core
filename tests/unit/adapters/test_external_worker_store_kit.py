"""ExternalWorkerStore kit: the docstring of `src/ports/external_worker_store.py` checked against
`FirestoreExternalWorkerStore` on the evaluating Firestore fake (index refusal on) and against the test fake
`tests/support/external_task_stores.py`. Assertions are on what readers return and, for Firestore, on the raw stored
documents.

`backend` is the one fixture that selects the store.
"""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_external_worker_store import FirestoreExternalWorkerStore
from src.domain.external_task import WorkerDestination
from src.domain.external_task_refusal import TaskRefusal
from src.domain.external_worker import ExternalWorker, WorkerStatus
from src.domain.external_worker_key import ExternalWorkerKey
from src.ports.external_worker_store import ExternalWorkerStore
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalWorkerStore

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
WORKERS = "eval_external_workers"
KEYS = "eval_external_worker_keys"
T0 = UTC_MIDNIGHT.utc


def worker(worker_id: str = "wrk-1", label: str = "research", **changes) -> ExternalWorker:
    base = dict(worker_id=worker_id, user_id="usr-1", account_id="acc-1", label=label, created_at=T0, updated_at=T0)
    base.update(changes)
    return ExternalWorker(**base)


def key(digest: str = "k" * 64, **changes) -> ExternalWorkerKey:
    base = dict(key_digest=digest, user_id="usr-1", account_id="acc-1", worker_id="wrk-1", hint="Zq4x", created_at=T0)
    base.update(changes)
    return ExternalWorkerKey(**base)


class Firestore:
    name = "firestore"

    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={WORKERS: "development_external_workers",
                                                       KEYS: "development_external_worker_keys"})
        env = SimpleNamespace(external_workers_collection=WORKERS, external_worker_keys_collection=KEYS)
        self.store: ExternalWorkerStore = FirestoreExternalWorkerStore(self.db, env)

    def written(self) -> object:
        return ({k: dict(v) for k, v in self.db.collection(WORKERS).docs.items()},
                {k: dict(v) for k, v in self.db.collection(KEYS).docs.items()})


class Fake:
    name = "fake"

    def __init__(self, kit_db=None) -> None:
        self.store = FakeExternalWorkerStore()

    def written(self) -> object:
        return dict(self.store.workers), dict(self.store.keys)


@pytest.fixture(params=[Firestore, Fake], ids=["firestore", "fake"])
def backend(request, kit_db):
    return request.param(kit_db)


async def test_create_and_read_a_worker(backend):
    assert await backend.store.create_worker(worker(description="web research"), 10) is None
    assert await backend.store.get_worker("usr-1", "wrk-1") == worker(description="web research")
    assert await backend.store.get_worker("usr-2", "wrk-1") is None
    assert await backend.store.find_active_by_label("usr-1", "research") == worker(description="web research")
    assert await backend.store.find_active_by_label("usr-1", "calendar-watch") is None
    assert await backend.store.find_active_by_label("usr-2", "research") is None


async def test_a_label_is_unique_among_active_workers_and_revoking_frees_it(backend):
    await backend.store.create_worker(worker(), 10)
    before = backend.written()
    assert await backend.store.create_worker(worker("wrk-2"), 10) is TaskRefusal.LABEL_TAKEN
    assert backend.written() == before
    assert await backend.store.create_worker(worker("wrk-3", user_id="usr-2"), 10) is None
    assert await backend.store.revoke_worker("usr-1", "wrk-1", T0 + timedelta(minutes=1)) is True
    assert await backend.store.revoke_worker("usr-1", "wrk-1", T0 + timedelta(minutes=2)) is False
    assert await backend.store.revoke_worker("usr-2", "wrk-1", T0 + timedelta(minutes=2)) is False
    revoked = await backend.store.get_worker("usr-1", "wrk-1")
    assert revoked.status is WorkerStatus.REVOKED and revoked.updated_at == T0 + timedelta(minutes=1)
    assert await backend.store.find_active_by_label("usr-1", "research") is None
    assert await backend.store.create_worker(worker("wrk-2", created_at=T0 + timedelta(minutes=3)), 10) is None
    assert [w.worker_id for w in await backend.store.list_workers("usr-1")] == ["wrk-1", "wrk-2"]


async def test_the_active_workers_cap(backend):
    await backend.store.create_worker(worker("wrk-1", "research"), 2)
    await backend.store.create_worker(worker("wrk-2", "calendar-watch"), 2)
    assert await backend.store.create_worker(worker("wrk-3", "export"), 2) is TaskRefusal.WORKERS_CAP
    await backend.store.revoke_worker("usr-1", "wrk-1", T0)
    assert await backend.store.create_worker(worker("wrk-3", "export"), 2) is None


async def test_keys(backend):
    assert await backend.store.create_key(key(), 2) is None
    assert await backend.store.get_key("k" * 64) == key()
    assert await backend.store.get_key("absent") is None
    with pytest.raises(AlreadyExists):
        await backend.store.create_key(key(hint="AAAA"), 5)
    assert await backend.store.create_key(key("m" * 64, created_at=T0 + timedelta(minutes=1)), 2) is None
    assert await backend.store.create_key(key("n" * 64), 2) is TaskRefusal.KEYS_CAP
    assert await backend.store.revoke_key("usr-2", "wrk-1", "k" * 64, T0) is False
    assert await backend.store.revoke_key("usr-1", "wrk-2", "k" * 64, T0) is False
    assert await backend.store.revoke_key("usr-1", "wrk-1", "k" * 64, T0 + timedelta(minutes=2)) is True
    assert await backend.store.revoke_key("usr-1", "wrk-1", "k" * 64, T0 + timedelta(minutes=3)) is False
    assert (await backend.store.get_key("k" * 64)).revoked_at == T0 + timedelta(minutes=2)
    assert await backend.store.create_key(key("n" * 64, created_at=T0 + timedelta(minutes=4)), 2) is None
    assert [k.key_digest for k in await backend.store.list_keys("usr-1", "wrk-1")] == ["k" * 64, "m" * 64, "n" * 64]
    assert await backend.store.list_keys("usr-2", "wrk-1") == []
    await backend.store.touch_key("m" * 64, T0 + timedelta(minutes=5))
    await backend.store.touch_key("absent", T0 + timedelta(minutes=5))
    assert (await backend.store.get_key("m" * 64)).last_used_at == T0 + timedelta(minutes=5)


async def test_the_stored_documents(kit_db):
    fs = Firestore(kit_db)
    await fs.store.create_worker(worker(destination=WorkerDestination.WORKSPACE), 10)
    await fs.store.create_key(key(), 2)
    assert fs.db.collection(WORKERS).docs == {"usr-1:wrk-1": {
        "worker_id": "wrk-1", "user_id": "usr-1", "account_id": "acc-1", "label": "research", "description": None,
        "destination": "workspace", "wake_host": None, "has_wake": False, "wake_secret": None, "wake_generation": 0,
        "status": "active", "created_at": T0, "updated_at": T0,
    }}
    assert fs.db.collection(KEYS).docs == {"k" * 64: {
        "user_id": "usr-1", "account_id": "acc-1", "worker_id": "wrk-1", "hint": "Zq4x", "created_at": T0,
        "last_used_at": None, "revoked_at": None,
    }}
