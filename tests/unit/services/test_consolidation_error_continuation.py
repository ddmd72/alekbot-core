"""An internal error in one batch does not stop the chain: the other open batches still get their continuation (worker
path: `has_more`; `$consolidate`: the enqueued continuation). The batch that broke is not retried by it, and an error
before any batch was read continues nothing (a failing store would loop hot)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.consolidation import BatchStatus, ConsolidationBatch
from src.domain.consolidation_run import ConsolidationRun
from src.services.consolidation_service import ConsolidationService
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"
ACC = "acc-1"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


class Coordinator:
    def __init__(self) -> None:
        self.routed: List[str] = []
        self.raising: set = set()

    async def route_message(self, message):
        batch_id = message.payload["batch_id"]
        self.routed.append(batch_id)
        if batch_id in self.raising:
            raise RuntimeError("coordinator down")
        return AgentResponse.success(result={}, agent_id="c", task_id=batch_id)


class Continuations:
    def __init__(self) -> None:
        self.users: List[str] = []

    async def enqueue_consolidation_task(self, user_id):
        self.users.append(user_id)

    async def enqueue_worker_task(self, *args, **kwargs):
        raise AssertionError("no deferral expected")


class AgentFactory:
    async def ensure_agents_for_user(self, user_id):
        return None


@pytest.fixture
def db():
    return EvalDb()


@pytest.fixture
def queue(db):
    return FirestoreConsolidationQueue(db, SimpleNamespace(consolidation_queue_collection="q"))


@pytest.fixture
def coordinator():
    return Coordinator()


@pytest.fixture
def continuations():
    return Continuations()


@pytest.fixture
def service(db, queue, coordinator, continuations):
    return ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=AgentFactory(),
        lease=FirestoreMemoryWriteLease(db, EnvironmentConfig()), run_timeout_s=900.0, task_queue=continuations,
        clock=lambda: NOW,
    )


async def _put(queue, *batch_ids):
    for created_at, batch_id in enumerate(batch_ids, start=1):
        await queue.enqueue_batch(ConsolidationBatch(
            batch_id=batch_id, user_id=USER, session_id="s", messages=[{"role": "user"}], created_at=float(created_at),
        ))


async def _status(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()["status"]


async def test_a_crash_while_routing_still_reports_the_other_batches_and_does_not_retry_the_broken_one(
    service, queue, coordinator,
):
    await _put(queue, "b1", "b2")
    coordinator.raising = {"b1"}

    has_more = await service.process_user_batches(USER)

    assert has_more is True
    assert coordinator.routed == ["b1"]
    assert await _status(queue, "b1") == BatchStatus.PROCESSING.value
    assert await _status(queue, "b2") == BatchStatus.PENDING.value


async def test_consolidate_now_enqueues_the_continuation_after_a_crash(service, queue, coordinator, continuations):
    await _put(queue, "b1", "b2")
    coordinator.raising = {"b1"}

    run = await service.consolidate_now(USER)

    assert run.has_more and continuations.users == [USER]
    assert coordinator.routed == ["b1"]


async def test_a_crash_with_no_other_batch_continues_nothing(service, queue, coordinator, continuations):
    await _put(queue, "b1")
    coordinator.raising = {"b1"}

    run = await service.consolidate_now(USER)

    assert not run.has_more and continuations.users == []


async def test_a_batch_that_broke_before_its_mark_is_not_its_own_continuation(
    service, queue, coordinator, continuations, monkeypatch,
):
    await _put(queue, "b1")
    original = queue.update_batch_status

    async def refuse_the_mark(batch_id, status, **kwargs):
        if status is BatchStatus.PROCESSING:
            raise RuntimeError("store down")
        return await original(batch_id, status, **kwargs)

    monkeypatch.setattr(queue, "update_batch_status", refuse_the_mark)

    run = await service.consolidate_now(USER)

    assert not run.has_more and continuations.users == []
    assert await _status(queue, "b1") == BatchStatus.PENDING.value and coordinator.routed == []


async def test_another_batch_still_continues_when_the_broken_one_is_left_pending(
    service, queue, continuations, monkeypatch,
):
    await _put(queue, "b1", "b2")
    original = queue.update_batch_status

    async def refuse_the_mark(batch_id, status, **kwargs):
        if status is BatchStatus.PROCESSING:
            raise RuntimeError("store down")
        return await original(batch_id, status, **kwargs)

    monkeypatch.setattr(queue, "update_batch_status", refuse_the_mark)

    run = await service.consolidate_now(USER)

    assert run.has_more and continuations.users == [USER]


async def test_an_error_before_any_batch_was_read_continues_nothing(service, queue, continuations, monkeypatch):
    await _put(queue, "b1", "b2")

    async def broken_reset(*args, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(queue, "reset_recoverable_batches", broken_reset)

    run = await service.consolidate_now(USER)

    assert not run.has_more and continuations.users == []


async def test_the_run_counts_a_crashed_batch_as_failed(service, queue, coordinator):
    await _put(queue, "b1")
    coordinator.raising = {"b1"}

    assert (await service.consolidate_now(USER)).failed == 1


async def test_the_run_counts_an_agent_failure_as_failed_and_a_clean_run_as_not(service, queue, coordinator):
    await _put(queue, "b1", "b2")
    original = coordinator.route_message

    calls = []

    async def failing_first(message):
        response = await original(message)
        calls.append(message.payload["batch_id"])
        if len(calls) == 1:
            return AgentResponse.failure(error="LLM 529", agent_id="c", task_id="b1")
        return response

    coordinator.route_message = failing_first

    failed_run = await service.consolidate_now(USER)
    clean_run = await service.consolidate_now(USER)

    assert failed_run.failed == 1 and failed_run.processed == 0
    assert (clean_run.failed, clean_run.processed) == (0, 1)


async def test_a_setup_error_is_a_failed_run(queue, coordinator, db):
    class BrokenFactory:
        async def ensure_agents_for_user(self, user_id):
            raise RuntimeError("agents down")

    service = ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=BrokenFactory(),
        lease=FirestoreMemoryWriteLease(db, EnvironmentConfig()), run_timeout_s=900.0, clock=lambda: NOW,
    )

    assert await service.consolidate_now(USER) == ConsolidationRun(failed=1)
