"""ConsolidationService under the per-account memory-write lease (FACT_LIFECYCLE_RFC D9, plan T6b).

The real lease adapter and the real queue adapter on one evaluating fake (H1); the task queue is a recording fake
that keeps Cloud Tasks' name semantics (a second task of the same name is ALREADY_EXISTS), the coordinator is the
scripted LLM boundary. Time is a settable clock shared by the service and the queue adapter.
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from src.adapters import firestore_consolidation_queue as queue_module
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.domain.consolidation_run import ConsolidationRun
from src.domain.request_context import get_current_account_id, get_current_user_id
from src.domain.memory_write_lease import LEASE_BUSY_RETRY_DELAY_S, LEASE_MARGIN, defer_bucket, lease_until
from src.services.consolidation_service import ConsolidationService
from tests.support.firestore_eval import EvalDb, EvalDocRef

USER = "user-abcdef123"
USER2 = "user-second-456"
ACC = "acc-1"
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
RUN_S = 900.0
LEASE_S = RUN_S + LEASE_MARGIN.total_seconds()          # 960 s
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingTaskQueue:
    """Records every enqueue call; a named worker task that already exists is ALREADY_EXISTS, answered None."""

    def __init__(self) -> None:
        self.attempts: List[Dict[str, Any]] = []
        self.worker_tasks: List[Dict[str, Any]] = []
        self.continuations: List[str] = []
        self.fail_worker_with: Optional[Exception] = None
        self._names: set = set()

    async def enqueue_worker_task(self, task_type, payload, delay_seconds=0, deadline_seconds=None, dedup_key=None):
        call = dict(task_type=task_type, payload=payload, delay_seconds=delay_seconds,
                    deadline_seconds=deadline_seconds, dedup_key=dedup_key)
        self.attempts.append(call)
        if self.fail_worker_with is not None:
            raise self.fail_worker_with
        name = f"{task_type}-{dedup_key}" if dedup_key else None
        if name is not None and name in self._names:
            return None
        if name is not None:
            self._names.add(name)
        self.worker_tasks.append(call)
        return name or "unnamed"

    async def enqueue_consolidation_task(self, user_id):
        self.continuations.append(user_id)
        return "continuation"


class Coordinator:
    """Scripted LLM boundary: records the messages, the lease document seen while routing, and runs a hook."""

    def __init__(self, db: EvalDb, env: EnvironmentConfig) -> None:
        self._leases = db.collection(env.memory_write_leases_collection)
        self.messages: List[Any] = []
        self.leases_seen: List[Optional[dict]] = []
        self.contexts_seen: List[tuple] = []
        self.failing: set = set()
        self.raising: set = set()
        self.on_route: Optional[Callable[[Any], Any]] = None

    @property
    def routed(self) -> List[str]:
        return [m.payload["batch_id"] for m in self.messages]

    async def route_message(self, message):
        self.messages.append(message)
        doc = self._leases.docs.get(ACC)
        self.leases_seen.append(dict(doc) if doc else None)
        self.contexts_seen.append((get_current_user_id(), get_current_account_id()))
        if self.on_route is not None:
            await self.on_route(message)
        batch_id = message.payload["batch_id"]
        if batch_id in self.raising:
            raise RuntimeError("coordinator down")
        if batch_id in self.failing:
            return AgentResponse.failure(error="LLM 529", agent_id="c", task_id=batch_id)
        return AgentResponse.success(result={}, agent_id="c", task_id=batch_id)


class AgentFactory:
    async def ensure_agents_for_user(self, user_id):
        return None


class UserRepo:
    def __init__(self, accounts: Dict[str, Optional[str]]) -> None:
        self._accounts = accounts

    async def get_user(self, user_id):
        return SimpleNamespace(account_id=self._accounts[user_id]) if user_id in self._accounts else None


@pytest.fixture
def env():
    return EnvironmentConfig()


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def _queue_time_follows_the_clock(monkeypatch, clock):
    monkeypatch.setattr(queue_module, "time", SimpleNamespace(time=lambda: clock.now.timestamp()))


@pytest.fixture
def queue(db):
    q_env = SimpleNamespace(consolidation_queue_collection="development_consolidation_queue")
    return FirestoreConsolidationQueue(db, q_env)


@pytest.fixture
def lease(db, env):
    return FirestoreMemoryWriteLease(db, env)


@pytest.fixture
def tasks():
    return RecordingTaskQueue()


@pytest.fixture
def coordinator(db, env):
    return Coordinator(db, env)


@pytest.fixture
def make_service(queue, lease, tasks, coordinator, clock):
    def build(**over):
        kwargs = dict(
            queue=queue, coordinator=coordinator, agent_factory=AgentFactory(), lease=lease, run_timeout_s=RUN_S,
            task_queue=tasks, user_repo=UserRepo({USER: ACC, USER2: ACC}), clock=clock,
        )
        kwargs.update(over)
        return ConsolidationService(**kwargs)
    return build


@pytest.fixture
def service(make_service):
    return make_service()


async def _put(queue, batch_id, created_at=1.0, status=BatchStatus.PENDING, user_id=USER, **extra):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=user_id, session_id="s", messages=[{"role": "user"}],
        status=status, created_at=created_at, **extra,
    ))


async def _doc(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()


def _lease_doc(db, env):
    return db.collection(env.memory_write_leases_collection).docs.get(ACC)


async def _hold(lease, owner="rival", at=T0, seconds=LEASE_S):
    assert await lease.acquire(ACC, owner, at, at + timedelta(seconds=seconds))


# ------------------------------------------------------------------------------------------------ the lease itself

@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
async def test_a_run_holds_the_account_lease_with_a_fresh_owner_per_batch_and_releases_it_after_success_failure_and_exception(
    service, queue, coordinator, db, env, outcome,
):
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)
    if outcome == "failure":
        coordinator.failing = {"b1"}
    if outcome == "exception":
        coordinator.raising = {"b1"}

    returned = await service.process_user_batches(USER)

    seen = coordinator.leases_seen
    assert all(doc is not None for doc in seen), "the lease must be held while the batch is routed"
    assert all(HEX32.match(doc["lease_owner"]) for doc in seen)
    assert all(doc["lease_until"] == T0 + timedelta(seconds=LEASE_S) for doc in seen)
    if outcome == "success":
        assert coordinator.routed == ["b1", "b2"]
        assert seen[0]["lease_owner"] != seen[1]["lease_owner"], "a fresh owner token per batch"
    else:
        assert coordinator.routed == ["b1"]          # a failed or crashed batch ends the run
    if outcome == "exception":
        assert returned is True, "a crash is swallowed; the other pending batch (b2) keeps the chain going"
    assert _lease_doc(db, env) is None, "released"


async def test_the_message_carries_the_batch_the_user_and_no_transient_retry(service, queue, coordinator):
    await _put(queue, "b1")

    await service.process_user_batches(USER, max_batches=1)

    assert coordinator.contexts_seen == [(USER, ACC)], "routed inside the user's request context"

    message = coordinator.messages[0]
    assert message.context == {"user_id": USER, "suppress_transient_retry": True}
    assert message.payload["task"] == "consolidate_full" and message.payload["messages"] == [{"role": "user"}]


class _TickingQueue(FirestoreConsolidationQueue):
    """The real queue, on a clock that moves while the run does its pre-agent work."""

    def __init__(self, db, env, clock, before_check: float, during_mark: float) -> None:
        super().__init__(db, env)
        self._clock, self._before_check, self._during_mark = clock, before_check, during_mark

    async def get_pending_batches(self, user_id, limit=10):
        batches = await super().get_pending_batches(user_id, limit)
        self._clock.advance(self._before_check)
        return batches

    async def update_batch_status(self, batch_id, status, error=None, facts_extracted=0):
        await super().update_batch_status(batch_id, status, error=error, facts_extracted=facts_extracted)
        if status == BatchStatus.PROCESSING:
            self._clock.advance(self._during_mark)


@pytest.mark.parametrize("before_check,during_mark,expected_timeout_ms", [
    (10, 3, int((LEASE_S - 60 - 13) * 1000)),     # lease_until - margin - now, read after the batch is marked
    (840, 0, 60_000),                             # exactly the margin left: still starts
    (841, 0, None),                               # under the margin left: starts nothing
])
async def test_message_timeout_is_lease_until_minus_margin_minus_now_and_under_a_margin_left_starts_nothing(
    make_service, db, clock, coordinator, tasks, env, before_check, during_mark, expected_timeout_ms,
):
    q_env = SimpleNamespace(consolidation_queue_collection="development_consolidation_queue")
    ticking = _TickingQueue(db, q_env, clock, before_check, during_mark)
    await _put(ticking, "b1")
    service = make_service(queue=ticking)

    await service.process_user_batches(USER, max_batches=1)

    if expected_timeout_ms is not None:
        assert [m.timeout_ms for m in coordinator.messages] == [expected_timeout_ms]
        return
    assert coordinator.messages == []
    doc = await _doc(ticking, "b1")
    assert doc["status"] == BatchStatus.PENDING.value and doc["processing_started_at"] is None
    assert doc["attempts"] == 0
    assert _lease_doc(db, env) is None, "released"
    assert [t["payload"] for t in tasks.worker_tasks] == [{"user_id": USER}], "a deferral instead"


# ------------------------------------------------------------------------------------------------ losers and deferral

def _spy_interleaving(monkeypatch, db):
    """Record the order of lease reads and commits, to prove two acquires overlapped."""
    events: List[str] = []
    real_get, real_commit = EvalDocRef.get, db._commit_writes

    async def spy_get(self, *args, **kwargs):
        if self.col.name.endswith("memory_write_leases"):
            events.append("read")
        return await real_get(self, *args, **kwargs)

    def spy_commit(writes, by_transaction=False):
        events.append("commit")
        return real_commit(writes, by_transaction=by_transaction)

    monkeypatch.setattr(EvalDocRef, "get", spy_get)
    monkeypatch.setattr(db, "_commit_writes", spy_commit)
    return events


async def test_second_concurrent_run_of_the_account_defers_with_a_named_bucket_task(
    service, queue, coordinator, tasks, db, env, monkeypatch,
):
    db.collection(env.memory_write_leases_collection).yield_on_get = True      # interleaving source
    events = _spy_interleaving(monkeypatch, db)
    await _put(queue, "b1")
    gate = asyncio.Event()

    async def hold_the_winner(_message):
        await gate.wait()
    coordinator.on_route = hold_the_winner

    winner = asyncio.create_task(service.process_user_batches(USER, max_batches=1))
    loser = asyncio.create_task(service.process_user_batches(USER, max_batches=1))
    loser_has_more = await loser                     # finishes while the winner still holds the lease
    gate.set()
    winner_has_more = await winner

    assert events[:2] == ["read", "read"], "both acquires must have read before the first commit"
    assert events.count("read") >= 3, "the loser's aborted transaction must have re-read and seen the lease held"
    assert coordinator.routed == ["b1"], "only the winner consolidates"
    assert loser_has_more is False and winner_has_more is False
    assert tasks.worker_tasks == [dict(
        task_type="consolidation", payload={"user_id": USER}, delay_seconds=LEASE_BUSY_RETRY_DELAY_S,
        deadline_seconds=1800, dedup_key=f"consolidation-defer:{USER}:{defer_bucket(T0)}",
    )]
    assert _lease_doc(db, env) is None


async def test_concurrent_losers_in_one_bucket_enqueue_one_task(service, queue, lease, tasks, clock):
    await _put(queue, "b1")
    await _hold(lease)

    runs = await asyncio.gather(*(service.consolidate_now(USER) for _ in range(3)))     # interleaving source: gather

    assert all(run == ConsolidationRun(deferred=True) for run in runs)
    assert len(tasks.attempts) == 3, "every loser tried to defer"
    assert len(tasks.worker_tasks) == 1, "the bucket's name collapsed them into one task"

    clock.advance(LEASE_BUSY_RETRY_DELAY_S)            # a loser that loses again lands in a later bucket
    await service.consolidate_now(USER)
    keys = [t["dedup_key"] for t in tasks.worker_tasks]
    assert len(keys) == 2 and keys[0] != keys[1]
    assert keys[1] == f"consolidation-defer:{USER}:{defer_bucket(T0) + 1}"


@pytest.mark.parametrize("status,defers", [
    (BatchStatus.PENDING, True),
    (BatchStatus.RETRY_PENDING, True),
    (BatchStatus.PROCESSING, True),
    (BatchStatus.FAILED, False),
    (None, False),
])
async def test_loser_defers_on_a_processing_batch_and_not_without_an_open_batch(
    service, queue, lease, tasks, coordinator, status, defers,
):
    if status is not None:
        await _put(queue, "b1", status=status, processing_started_at=T0.timestamp())
    await _hold(lease)

    run = await service.consolidate_now(USER)

    assert coordinator.messages == []
    assert len(tasks.worker_tasks) == (1 if defers else 0)
    assert run == ConsolidationRun(deferred=defers)


async def test_holder_crash_then_retry_defers_and_the_batch_runs_after_lease_expiry(
    service, queue, lease, tasks, coordinator, clock, db, env,
):
    acquired_at = T0
    expiry = lease_until(acquired_at, RUN_S)
    await _hold(lease, "dead-holder", at=acquired_at)                            # the holder crashed: no release
    await _put(queue, "b1", status=BatchStatus.PROCESSING, processing_started_at=(acquired_at + timedelta(seconds=5)).timestamp())

    clock.now = acquired_at + timedelta(seconds=30)                              # a Cloud Tasks retry, before expiry
    assert await service.process_user_batches(USER, max_batches=1) is False
    assert coordinator.messages == [] and len(tasks.worker_tasks) == 1
    assert (await _doc(queue, "b1"))["status"] == BatchStatus.PROCESSING.value

    clock.now = expiry + timedelta(seconds=1)                                    # the deferred task wins the free lease
    age = clock.now.timestamp() - (await _doc(queue, "b1"))["processing_started_at"]
    assert age == 956, "the batch is younger than the old 960 s threshold"
    assert await queue.reset_recoverable_batches(USER, zombie_after_s=960) == 0, "the old value would have missed it"
    assert await service.process_user_batches(USER, max_batches=1) is False

    assert coordinator.routed == ["b1"], "reset under the 60 s threshold, then run"
    assert await _doc(queue, "b1") is None
    assert _lease_doc(db, env) is None


async def test_a_live_batch_of_another_run_is_never_reset_by_a_lease_holder(service, queue, coordinator, clock):
    """Under a fresh lease a PROCESSING batch younger than the margin can only be our own run's (the guard's lower edge)."""
    await _put(queue, "b1", status=BatchStatus.PROCESSING, processing_started_at=clock.now.timestamp() - 59)

    await service.process_user_batches(USER, max_batches=1)

    assert coordinator.messages == []
    assert (await _doc(queue, "b1"))["status"] == BatchStatus.PROCESSING.value


async def test_a_run_that_loses_the_lease_between_batches_defers_the_rest(
    make_service, queue, lease, coordinator, tasks, db, env,
):
    class RivalTakesItBetweenBatches(FirestoreMemoryWriteLease):
        acquires = 0

        async def acquire(self, account_id, owner, now, until):
            RivalTakesItBetweenBatches.acquires += 1
            if RivalTakesItBetweenBatches.acquires == 2:
                assert await super().acquire(account_id, "rival", now, until)
            return await super().acquire(account_id, owner, now, until)

    service = make_service(lease=RivalTakesItBetweenBatches(db, env))
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    assert await service.process_user_batches(USER) is False

    assert coordinator.routed == ["b1"]
    assert await _doc(queue, "b1") is None
    assert (await _doc(queue, "b2"))["status"] == BatchStatus.PENDING.value
    assert len(tasks.worker_tasks) == 1
    assert _lease_doc(db, env)["lease_owner"] == "rival", "the rival's lease is not touched by our release"


# ------------------------------------------------------------------------------------------------ account guard

async def test_falsy_account_fails_the_batch_and_raises_nothing(make_service, queue, db, env, coordinator, tasks, caplog):
    acquires: List[Any] = []

    class CountingLease(FirestoreMemoryWriteLease):
        async def acquire(self, *args, **kwargs):
            acquires.append(args)
            return await super().acquire(*args, **kwargs)

    service = make_service(lease=CountingLease(db, env), user_repo=UserRepo({USER: None}))
    await _put(queue, "b1", attempts=1)

    with caplog.at_level(logging.ERROR):
        has_more = await service.process_user_batches(USER)

    assert has_more is False
    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.FAILED.value and doc["last_error"] == "user has no account"
    assert acquires == [] and coordinator.messages == [] and tasks.attempts == []
    failed = [r.getMessage() for r in caplog.records if "CONSOLIDATION_FAILED" in r.getMessage()]
    assert len(failed) == 1 and "batch_id=b1" in failed[0] and "attempts=1" in failed[0]


async def test_falsy_account_with_no_batch_does_nothing(make_service, db, env, coordinator):
    service = make_service(user_repo=UserRepo({USER: ""}))

    assert await service.process_user_batches(USER) is False
    assert coordinator.messages == []


async def test_a_user_without_a_profile_fails_the_batch_and_never_leases_under_the_user_id(
    make_service, queue, coordinator, db, env, tasks,
):
    # owner, 2026-10-23: personal data is never written under the user id; the batch fails instead
    service = make_service(user_repo=UserRepo({}))
    await _put(queue, "b1")

    await service.process_user_batches(USER, max_batches=1)

    assert coordinator.routed == [] and tasks.attempts == []
    doc = await _doc(queue, "b1")
    assert doc["status"] == BatchStatus.FAILED.value and doc["last_error"] == "user has no profile"
    assert db.collection(env.memory_write_leases_collection).txn_versions.get(USER) is None


# ------------------------------------------------------------------------------------------------ no task queue

async def test_consolidate_now_without_a_task_queue_warns_on_has_more(make_service, queue, caplog):
    service = make_service(task_queue=None)
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    with caplog.at_level(logging.WARNING):
        run = await service.consolidate_now(USER)

    assert run == ConsolidationRun(processed=1, has_more=True, unattended=True)
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "no queue for the continuation" in r.getMessage()]
    assert (await _doc(queue, "b2"))["status"] == BatchStatus.PENDING.value


async def test_busy_without_a_task_queue_logs_a_warning_and_returns_deferred(make_service, queue, lease, caplog):
    service = make_service(task_queue=None)
    await _put(queue, "b1")
    await _hold(lease)

    with caplog.at_level(logging.WARNING):
        run = await service.consolidate_now(USER)

    assert run == ConsolidationRun(deferred=True, unattended=True)
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "no queue to defer to" in r.getMessage()]


async def test_busy_without_a_task_queue_and_nothing_open_is_silent(make_service, lease, caplog):
    service = make_service(task_queue=None)
    await _hold(lease)

    with caplog.at_level(logging.WARNING):
        run = await service.consolidate_now(USER)

    assert run == ConsolidationRun()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ------------------------------------------------------------------------------------------------ error policy

async def test_acquire_or_enqueue_errors_propagate_and_release_errors_are_swallowed(
    make_service, queue, lease, tasks, coordinator, db, env, caplog,
):
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    class AcquireDown(FirestoreMemoryWriteLease):
        async def acquire(self, *args, **kwargs):
            raise ConnectionError("lease store down")

    with pytest.raises(ConnectionError):
        await make_service(lease=AcquireDown(db, env)).process_user_batches(USER)
    assert coordinator.messages == []
    assert (await _doc(queue, "b1"))["status"] == BatchStatus.PENDING.value, "nothing was started"

    await _hold(lease)
    tasks.fail_worker_with = TimeoutError("cloud tasks down")
    with pytest.raises(TimeoutError):
        await make_service().process_user_batches(USER)

    class OpenBatchesDown(FirestoreConsolidationQueue):
        async def has_open_batches(self, user_id):
            raise ConnectionError("queue read down")

    with pytest.raises(ConnectionError):
        await make_service(queue=OpenBatchesDown(db, SimpleNamespace(
            consolidation_queue_collection="development_consolidation_queue"))).process_user_batches(USER)

    class ReleaseDown(FirestoreMemoryWriteLease):
        async def release(self, *args, **kwargs):
            raise ConnectionError("lease store down")

    # a free lease again; the batch succeeds, the release fails, and has_more survives
    db.collection(env.memory_write_leases_collection).docs.clear()
    with caplog.at_level(logging.WARNING):
        has_more = await make_service(lease=ReleaseDown(db, env)).process_user_batches(USER, max_batches=1)

    assert has_more is True
    assert coordinator.routed == ["b1"] and await _doc(queue, "b1") is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "releasing" in r.getMessage()]
    assert len(warnings) == 1 and ACC in warnings[0]


async def test_a_deferral_enqueue_error_in_the_no_time_path_propagates_and_releases(
    make_service, db, clock, tasks, env,
):
    q_env = SimpleNamespace(consolidation_queue_collection="development_consolidation_queue")
    ticking = _TickingQueue(db, q_env, clock, before_check=900, during_mark=0)
    await _put(ticking, "b1")
    tasks.fail_worker_with = TimeoutError("cloud tasks down")

    with pytest.raises(TimeoutError):
        await make_service(queue=ticking).process_user_batches(USER)

    assert _lease_doc(db, env) is None


async def test_an_error_inside_the_batch_body_is_swallowed_and_the_lease_released(make_service, db, env, coordinator, queue):
    class ResetDown(FirestoreConsolidationQueue):
        async def reset_recoverable_batches(self, *args, **kwargs):
            raise RuntimeError("Firestore down")

    down = ResetDown(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))
    await _put(down, "b1")
    service = make_service(queue=down)

    assert await service.process_user_batches(USER) is False, "a pending batch remains but the crash reports no continuation"
    assert coordinator.messages == []
    assert _lease_doc(db, env) is None


async def test_a_setup_error_is_swallowed_as_before_and_nothing_is_leased(make_service, queue, coordinator, db, env, caplog):
    class FactoryDown(AgentFactory):
        async def ensure_agents_for_user(self, user_id):
            raise RuntimeError("agent build failed")

    await _put(queue, "b1")

    with caplog.at_level(logging.ERROR):
        assert await make_service(agent_factory=FactoryDown()).process_user_batches(USER) is False

    assert [r for r in caplog.records if "Unhandled error in batch processing" in r.getMessage()]
    assert coordinator.messages == [] and _lease_doc(db, env) is None
    assert (await _doc(queue, "b1"))["status"] == BatchStatus.PENDING.value


async def test_a_failing_has_more_read_after_the_batch_reports_a_continuation_and_loses_none(make_service, db, queue, coordinator):
    class SecondReadDown(FirestoreConsolidationQueue):
        reads = 0

        async def get_pending_batches(self, user_id, limit=10):
            SecondReadDown.reads += 1
            if SecondReadDown.reads == 2:
                raise ConnectionError("queue read down")
            return await super().get_pending_batches(user_id, limit)

    flaky = SecondReadDown(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))
    await _put(flaky, "b1", 1.0)
    await _put(flaky, "b2", 2.0)

    assert await make_service(queue=flaky).process_user_batches(USER, max_batches=1) is True

    assert coordinator.routed == ["b1"] and await _doc(flaky, "b1") is None
    assert (await _doc(flaky, "b2"))["status"] == BatchStatus.PENDING.value


async def test_consolidate_now_queues_the_continuation_when_the_has_more_read_fails(make_service, db, queue, tasks):
    class SecondReadDown(FirestoreConsolidationQueue):
        reads = 0

        async def get_pending_batches(self, user_id, limit=10):
            SecondReadDown.reads += 1
            if SecondReadDown.reads == 2:
                raise ConnectionError("queue read down")
            return await super().get_pending_batches(user_id, limit)

    flaky = SecondReadDown(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))
    await _put(flaky, "b1")

    run = await make_service(queue=flaky).consolidate_now(USER)

    assert run == ConsolidationRun(processed=1, has_more=True)
    assert tasks.continuations == [USER]


@pytest.mark.parametrize("run_timeout_s", [60.0, 59.0, 0, -1])
def test_the_constructor_refuses_a_run_timeout_that_leaves_no_time_after_the_margin(make_service, run_timeout_s):
    with pytest.raises(ValueError):
        make_service(run_timeout_s=run_timeout_s)


def test_the_constructor_accepts_a_run_timeout_just_over_the_margin(make_service):
    assert make_service(run_timeout_s=60.1) is not None


# ------------------------------------------------------------------------------------------------ one writer per account

async def test_two_users_of_one_account_are_serialised(service, queue, coordinator, tasks, db, env):
    await _put(queue, "a1", user_id=USER)
    await _put(queue, "b1", user_id=USER2)
    gate, reached = asyncio.Event(), asyncio.Event()

    async def hold_the_first(message):
        if message.payload["batch_id"] == "a1":
            reached.set()
            await gate.wait()
    coordinator.on_route = hold_the_first

    first = asyncio.create_task(service.process_user_batches(USER, max_batches=1))
    await asyncio.wait_for(reached.wait(), timeout=5)
    assert coordinator.routed == ["a1"], "the first user holds the account's lease"
    second_has_more = await service.process_user_batches(USER2, max_batches=1)
    gate.set()
    await first

    assert second_has_more is False
    assert coordinator.routed == ["a1"], "the second user's batch did not run beside it"
    assert [t["payload"] for t in tasks.worker_tasks] == [{"user_id": USER2}]

    await service.process_user_batches(USER2, max_batches=1)
    assert coordinator.routed == ["a1", "b1"]


async def test_consolidate_now_processes_one_batch_and_queues_the_rest(service, queue, coordinator, tasks):
    for i, name in enumerate(["b1", "b2", "b3"]):
        await _put(queue, name, float(i + 1))

    run = await service.consolidate_now(USER)

    assert run == ConsolidationRun(processed=1, has_more=True, deferred=False)
    assert coordinator.routed == ["b1"]
    assert tasks.continuations == [USER] and tasks.worker_tasks == []
    assert [(await _doc(queue, n)) is None for n in ["b1", "b2", "b3"]] == [True, False, False]


async def test_consolidate_now_with_nothing_left_queues_nothing(service, queue, tasks):
    await _put(queue, "b1")

    run = await service.consolidate_now(USER)

    assert run == ConsolidationRun(processed=1)
    assert tasks.continuations == []


async def test_deferral_returns_false_from_process_user_batches(service, queue, lease, tasks, coordinator):
    await _put(queue, "b1")
    await _hold(lease)

    result = await service.process_user_batches(USER, max_batches=1)

    assert result is False
    assert coordinator.messages == [] and len(tasks.worker_tasks) == 1
    assert tasks.continuations == [], "the worker must not enqueue at once"


async def test_a_success_returns_the_has_more_of_the_queue(service, queue):
    await _put(queue, "b1", 1.0)
    await _put(queue, "b2", 2.0)

    assert await service.process_user_batches(USER, max_batches=1) is True
    assert await service.process_user_batches(USER, max_batches=1) is False
