"""ExternalTaskStore kit: the docstring of `src/ports/external_task_store.py` checked against `FirestoreExternalTaskStore` on
the evaluating Firestore fake (index refusal on) and against the test fake `tests/support/external_task_stores.py`.
Assertions are on what readers return and, for Firestore, on the raw stored documents. The rules themselves are
`tests/unit/domain/test_external_task_rules.py`; this kit shows each store applies them in one transaction.

`backend` is the one fixture that selects the store.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists

from src.adapters.firestore_external_task_store import FirestoreExternalTaskStore
from src.domain.external_task import (
    LEASE_S,
    RETENTION_S,
    DeliveryState,
    ExternalTask,
    ExternalTaskStatus,
    SubmitVia,
    TaskDelivery,
    TaskHold,
    TaskLease,
    TaskOrigin,
    TaskResultMeta,
    WorkerDestination,
)
from src.domain.external_task_refusal import TaskRefusal
from src.ports.external_task_store import ExternalTaskStore
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalTaskStore

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
TASKS = "eval_external_tasks"
RESULTS = "eval_external_task_results"
T0 = UTC_MIDNIGHT.utc
S = ExternalTaskStatus
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TEXT = "compare three community-garden irrigation kits for Shelbyville"


def task(task_id: str = "tsk-1", **changes) -> ExternalTask:
    base = dict(task_id=task_id, user_id="usr-1", account_id="acc-1", worker_id="wrk-1", label="research",
                title="Irrigation kits", text=TEXT, origin=ORIGIN, status=S.QUEUED, created_at=T0, updated_at=T0,
                deadline_at=T0 + timedelta(days=7))
    base.update(changes)
    return ExternalTask(**base)


def meta(sha: str = "a" * 64) -> TaskResultMeta:
    return TaskResultMeta(bytes=12, sha256=sha, submitted_at=T0 + timedelta(seconds=5), submitted_via=SubmitVia.KEY)


class Firestore:
    name = "firestore"

    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={TASKS: "development_external_tasks",
                                                       RESULTS: "development_external_task_results"})
        env = SimpleNamespace(external_tasks_collection=TASKS, external_task_results_collection=RESULTS)
        self.store: ExternalTaskStore = FirestoreExternalTaskStore(self.db, env)

    def docs(self) -> dict:
        return self.db.collection(TASKS).docs

    def results(self) -> dict:
        return self.db.collection(RESULTS).docs

    def written(self) -> object:
        return ({k: dict(v) for k, v in self.docs().items()}, {k: dict(v) for k, v in self.results().items()})


class Fake:
    name = "fake"

    def __init__(self, kit_db=None) -> None:
        self.store = FakeExternalTaskStore()

    def written(self) -> object:
        return dict(self.store.tasks), dict(self.store.results)


@pytest.fixture(params=[Firestore, Fake], ids=["firestore", "fake"])
def backend(request, kit_db):
    return request.param(kit_db)


async def _leased(backend, task_id: str = "tsk-1", **changes) -> ExternalTask:
    await backend.store.create(task(task_id, **changes))
    out = await backend.store.claim("usr-1", task_id, worker_id="wrk-1", expected_status=S.QUEUED,
                                    expected_generation=0, lease_id="lease-a", now=T0 + timedelta(seconds=1))
    assert out.written
    return out.task


async def test_create_get_and_tenancy(backend):
    await backend.store.create(task())
    assert await backend.store.get("usr-1", "tsk-1") == task()
    assert await backend.store.get("usr-2", "tsk-1") is None
    assert await backend.store.get("usr-1", "absent") is None
    with pytest.raises(AlreadyExists):
        await backend.store.create(task(title="Another title"))
    assert (await backend.store.get("usr-1", "tsk-1")).title == "Irrigation kits"


async def test_every_field_round_trips(backend):
    full = task(status=S.DONE, hold=TaskHold(code_digest="d" * 64, hold_until=T0 + timedelta(hours=1)),
                lease=TaskLease(lease_id="lease-a", until=T0 + timedelta(seconds=LEASE_S), generation=3, renewals=2),
                claims=2, result_meta=meta(), delivery=TaskDelivery(state=DeliveryState.DELIVERED,
                                                                     destination=WorkerDestination.WORKSPACE,
                                                                     path="references/external_tasks/x.md",
                                                                     delivered_at=T0 + timedelta(minutes=1)),
                purge_after=T0 + timedelta(days=30))
    await backend.store.create(full)
    assert await backend.store.get("usr-1", "tsk-1") == full


async def test_listings_are_scoped_and_ordered(backend):
    await backend.store.create(task("tsk-1"))
    await backend.store.create(task("tsk-2", created_at=T0 + timedelta(minutes=1), status=S.HELD))
    await backend.store.create(task("tsk-3", created_at=T0 + timedelta(minutes=2), worker_id="wrk-2", status=S.DONE))
    await backend.store.create(task("tsk-4", created_at=T0 + timedelta(minutes=3), user_id="usr-2"))
    assert [t.task_id for t in await backend.store.list_for_user("usr-1", 10)] == ["tsk-3", "tsk-2", "tsk-1"]
    assert [t.task_id for t in await backend.store.list_for_user("usr-1", 2)] == ["tsk-3", "tsk-2"]
    visible = await backend.store.list_for_worker("usr-1", "wrk-1", [S.QUEUED, S.LEASED])
    assert [t.task_id for t in visible] == ["tsk-1"]
    assert await backend.store.list_for_worker("usr-1", "wrk-1", []) == []
    assert await backend.store.count_open("usr-1") == 2
    assert await backend.store.count_open("usr-2") == 1


async def test_find_held_matches_the_digest_of_held_tasks_only(backend):
    hold = TaskHold(code_digest="d" * 64, hold_until=T0 + timedelta(hours=1))
    await backend.store.create(task("tsk-1", status=S.HELD, hold=hold))
    await backend.store.create(task("tsk-2", status=S.HELD, hold=TaskHold(code_digest="e" * 64,
                                                                          hold_until=T0 + timedelta(hours=1))))
    await backend.store.create(task("tsk-3", status=S.HELD, hold=hold, user_id="usr-2"))
    assert [t.task_id for t in await backend.store.find_held("usr-1", "d" * 64)] == ["tsk-1"]
    assert await backend.store.find_held("usr-1", "f" * 64) == []


async def test_claim_candidates(backend):
    await backend.store.create(task("tsk-1", created_at=T0 + timedelta(minutes=2)))
    await backend.store.create(task("tsk-2", created_at=T0 + timedelta(minutes=1)))
    await backend.store.create(task("tsk-3", worker_id="wrk-2"))
    await backend.store.create(task("tsk-4", status=S.LEASED,
                                    lease=TaskLease(lease_id="l4", until=T0 + timedelta(minutes=5), generation=1)))
    await backend.store.create(task("tsk-5", status=S.LEASED,
                                    lease=TaskLease(lease_id="l5", until=T0 + timedelta(minutes=20), generation=1)))
    queued = await backend.store.queued_candidates("usr-1", "wrk-1", 10)
    assert [t.task_id for t in queued] == ["tsk-2", "tsk-1"]
    assert [t.task_id for t in await backend.store.queued_candidates("usr-1", "wrk-1", 1)] == ["tsk-2"]
    lapsed = await backend.store.lapsed_candidates("usr-1", "wrk-1", T0 + timedelta(minutes=10), 10)
    assert [t.task_id for t in lapsed] == ["tsk-4"]
    assert await backend.store.lapsed_candidates("usr-2", "wrk-1", T0 + timedelta(hours=1), 10) == []


async def test_a_transition_writes_the_rules_outcome_and_a_refusal_writes_nothing(backend):
    leased = await _leased(backend)
    assert leased.status is S.LEASED and leased.lease.lease_id == "lease-a" and leased.claims == 1
    assert await backend.store.get("usr-1", "tsk-1") == leased
    before = backend.written()
    stale = await backend.store.claim("usr-1", "tsk-1", worker_id="wrk-1", expected_status=S.QUEUED,
                                      expected_generation=0, lease_id="lease-b", now=T0 + timedelta(seconds=2))
    assert stale.refusal is TaskRefusal.CONTENDED and not stale.written
    foreign = await backend.store.renew("usr-2", "tsk-1", worker_id="wrk-1", lease_id="lease-a",
                                        now=T0 + timedelta(seconds=3))
    assert foreign.refusal is TaskRefusal.NOT_FOUND and foreign.task is None
    missing = await backend.store.cancel("usr-1", "absent", now=T0)
    assert missing.refusal is TaskRefusal.NOT_FOUND
    assert backend.written() == before


async def test_submit_creates_the_result_in_the_same_transaction_and_a_replay_writes_nothing(backend):
    await _leased(backend)
    out = await backend.store.submit("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a", meta=meta(),
                                     result_text="Kit B waters evenly.", now=T0 + timedelta(seconds=5))
    assert out.written and out.task.status is S.DONE
    assert await backend.store.get_result("usr-1", "tsk-1") == "Kit B waters evenly."
    assert await backend.store.get_result("usr-2", "tsk-1") is None
    before = backend.written()
    replay = await backend.store.submit("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a", meta=meta(),
                                        result_text="Kit B waters evenly.", now=T0 + timedelta(seconds=9))
    assert replay.ok and not replay.written
    other = await backend.store.submit("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a", meta=meta("b" * 64),
                                       result_text="Kit C.", now=T0 + timedelta(seconds=9))
    assert other.refusal is TaskRefusal.ALREADY_SUBMITTED
    assert backend.written() == before


async def test_an_existing_result_record_aborts_the_submit(backend):
    await _leased(backend)
    if isinstance(backend, Firestore):
        await backend.db.collection(RESULTS).document("tsk-1").set({"text": "planted", "purge_after": None})
    else:
        backend.store.results["tsk-1"] = ("planted", None)
    with pytest.raises(AlreadyExists):
        await backend.store.submit("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a", meta=meta(),
                                   result_text="Kit B.", now=T0 + timedelta(seconds=5))
    assert (await backend.store.get("usr-1", "tsk-1")).status is S.LEASED
    assert await backend.store.get_result("usr-1", "tsk-1") == "planted"


async def test_each_transition_reaches_its_rule(backend):
    await _leased(backend)
    renewed = await backend.store.renew("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a",
                                        now=T0 + timedelta(seconds=60))
    assert renewed.task.lease.renewals == 1
    released = await backend.store.release("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a",
                                           now=T0 + timedelta(seconds=61))
    assert released.task.status is S.QUEUED and released.task.lease.generation == 2
    await backend.store.claim("usr-1", "tsk-1", worker_id="wrk-1", expected_status=S.QUEUED, expected_generation=2,
                              lease_id="lease-b", now=T0 + timedelta(seconds=62))
    failed = await backend.store.fail("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-b", reason="no stock",
                                      now=T0 + timedelta(seconds=63))
    assert failed.task.status is S.FAILED and failed.task.failure.reason == "no stock"
    assert failed.task.purge_after == T0 + timedelta(seconds=63 + RETENTION_S)
    delivery = TaskDelivery(state=DeliveryState.DELIVERED, delivered_at=T0 + timedelta(seconds=70))
    delivered = await backend.store.record_delivery("usr-1", "tsk-1", delivery=delivery)
    assert delivered.written and (await backend.store.get("usr-1", "tsk-1")).delivery == delivery

    hold = TaskHold(code_digest="d" * 64, hold_until=T0 + timedelta(hours=1))
    await backend.store.create(task("tsk-2", status=S.HELD, hold=hold))
    confirmed = await backend.store.confirm("usr-1", "tsk-2", code_digest="d" * 64, now=T0 + timedelta(minutes=1))
    assert confirmed.task.status is S.QUEUED
    cancelled = await backend.store.cancel("usr-1", "tsk-2", now=T0 + timedelta(minutes=2))
    assert cancelled.task.status is S.CANCELLED

    await backend.store.create(task("tsk-3", status=S.HELD, hold=hold))
    assert (await backend.store.expire_hold("usr-1", "tsk-3", now=T0 + timedelta(hours=1))).task.status is S.EXPIRED
    await backend.store.create(task("tsk-4"))
    assert (await backend.store.expire_deadline("usr-1", "tsk-4", now=T0 + timedelta(days=7))).task.status is S.EXPIRED
    await _leased(backend, "tsk-5")
    lapsed = await backend.store.expire_lease("usr-1", "tsk-5", generation=1,
                                              now=T0 + timedelta(seconds=1 + LEASE_S))
    assert lapsed.task.status is S.QUEUED


async def test_concurrent_claims_of_one_task_one_wins(backend):
    await backend.store.create(task())

    async def claim(lease_id: str):
        return await backend.store.claim("usr-1", "tsk-1", worker_id="wrk-1", expected_status=S.QUEUED,
                                         expected_generation=0, lease_id=lease_id, now=T0 + timedelta(seconds=1))

    results = await asyncio.gather(claim("lease-a"), claim("lease-b"))
    assert sorted(r.written for r in results) == [False, True]
    winner = next(r for r in results if r.written)
    assert (await backend.store.get("usr-1", "tsk-1")).lease.lease_id == winner.task.lease.lease_id
    assert next(r for r in results if not r.written).refusal is TaskRefusal.CONTENDED


@pytest.mark.h1_only("drives interfere_on_txn_read: another transaction renews right after this one read the task")
async def test_a_timer_that_loses_to_a_renewal_re_runs_on_the_renewed_lease(kit_db):
    fs = Firestore(kit_db)
    leased = await _leased(fs)
    late = T0 + timedelta(seconds=1 + LEASE_S)
    renewed_until = late + timedelta(seconds=LEASE_S)
    fs.db.interfere_on_txn_read(TASKS, "tsk-1", {"lease": {**fs.docs()["tsk-1"]["lease"], "until": renewed_until,
                                                           "renewals": 1}}, after_read=True)
    out = await fs.store.expire_lease("usr-1", "tsk-1", generation=leased.lease.generation, now=late)
    assert out.refusal is TaskRefusal.NOT_DUE
    assert fs.docs()["tsk-1"]["status"] == "leased" and fs.docs()["tsk-1"]["lease"]["until"] == renewed_until


async def test_the_stored_documents(kit_db):
    fs = Firestore(kit_db)
    await _leased(fs)
    await fs.store.submit("usr-1", "tsk-1", worker_id="wrk-1", lease_id="lease-a", meta=meta(),
                          result_text="Kit B waters evenly.", now=T0 + timedelta(seconds=5))
    purge = T0 + timedelta(seconds=5 + RETENTION_S)
    assert fs.docs() == {"tsk-1": {
        "user_id": "usr-1", "account_id": "acc-1", "worker_id": "wrk-1", "label": "research",
        "title": "Irrigation kits", "text": TEXT,
        "origin": {"platform": "slack", "channel_id": "C-garden", "session_id": "usr-1:C-garden"},
        "status": "done", "hold": None,
        "lease": {"lease_id": "lease-a", "until": T0 + timedelta(seconds=1 + LEASE_S), "generation": 1, "renewals": 0},
        "claims": 1,
        "result_meta": {"bytes": 12, "sha256": "a" * 64, "submitted_at": T0 + timedelta(seconds=5),
                        "submitted_via": "key", "client_id": None},
        "failure": None, "delivery": None, "created_tainted": False, "created_at": T0, "updated_at": T0 + timedelta(seconds=5),
        "deadline_at": T0 + timedelta(days=7), "purge_after": purge,
    }}
    assert fs.results() == {"tsk-1": {"text": "Kit B waters evenly.", "purge_after": purge}}


async def test_a_stored_task_that_still_carries_created_tainted_loads_and_the_field_is_dropped_at_the_next_write(kit_db):
    fs = Firestore(kit_db)
    await fs.store.create(task())
    fs.docs()["tsk-1"]["created_tainted"] = True
    assert await fs.store.get("usr-1", "tsk-1") == task()
    out = await fs.store.claim("usr-1", "tsk-1", worker_id="wrk-1", expected_status=S.QUEUED, expected_generation=0,
                               lease_id="lease-a", now=T0 + timedelta(seconds=1))
    assert out.written and "created_tainted" not in fs.docs()["tsk-1"]
