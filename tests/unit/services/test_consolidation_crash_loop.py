"""A batch whose run kills the worker is failed after MAX_BATCH_ATTEMPTS lost runs, not reset for ever.

Real ConsolidationService, lease and queue on one evaluating fake; the coordinator "crashes" with a BaseException that
the service's `except Exception` does not catch, leaving the batch PROCESSING exactly as a dead instance would.
"""
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.adapters import firestore_consolidation_queue as queue_module
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.consolidation import MAX_BATCH_ATTEMPTS, MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from src.domain.memory_write_lease import lease_until
from src.domain.user import UserBotConfig
from src.services.consolidation_service import ConsolidationService
from tests.support.account_owners import owner_zones
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"
T0 = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
RUN_S = 900.0


class _Crash(BaseException):
    """The instance dies: nothing in the run's own error handling sees it."""


class _Coordinator:
    def __init__(self):
        self.routed = []
        self.crash = True

    async def route_message(self, message):
        self.routed.append(message.payload["batch_id"])
        if self.crash:
            raise _Crash()
        return AgentResponse.success(result={}, agent_id="c", task_id="t")


class _Factory:
    async def ensure_agents_for_user(self, user_id):
        return None


class _Profiles:
    async def get_user(self, user_id):
        return SimpleNamespace(account_id="acc-1", config=UserBotConfig())


@pytest.fixture
def world(monkeypatch):
    db = EvalDb()
    state = SimpleNamespace(now=T0)
    monkeypatch.setattr(queue_module, "time", SimpleNamespace(time=lambda: state.now.timestamp()))
    queue = FirestoreConsolidationQueue(db, SimpleNamespace(consolidation_queue_collection="development_consolidation_queue"))
    coordinator = _Coordinator()
    profiles = _Profiles()
    service = ConsolidationService(
        queue=queue, coordinator=coordinator, agent_factory=_Factory(),
        lease=FirestoreMemoryWriteLease(db, EnvironmentConfig()), run_timeout_s=RUN_S, user_repo=profiles,
        owner_zones=owner_zones({"acc-1": USER}, profiles),
        clock=lambda: state.now,
    )
    return SimpleNamespace(queue=queue, coordinator=coordinator, service=service, state=state, db=db)


async def _put(queue, batch_id="b1"):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=USER, session_id="s", messages=[{"role": "user"}], created_at=1.0,
    ))


async def _doc(queue, batch_id="b1"):
    return (await queue.collection.document(batch_id).get()).to_dict()


async def _crashing_run(w):
    """One worker task that dies mid-batch, then the lease runs out and the next winner arrives."""
    try:
        await w.service.process_user_batches(USER, max_batches=1)
    except _Crash:
        pass
    w.state.now += timedelta(seconds=lease_until(T0, RUN_S).timestamp() - T0.timestamp() + 1)


async def test_after_the_bound_the_crashing_batch_is_failed_and_never_run_again(world, caplog):
    await _put(world.queue)

    with caplog.at_level(logging.ERROR):
        for _ in range(MAX_BATCH_ATTEMPTS + 3):          # more tasks than the bound allows
            await _crashing_run(world)

    assert world.coordinator.routed == ["b1"] * MAX_BATCH_ATTEMPTS
    doc = await _doc(world.queue)
    assert doc["status"] == BatchStatus.FAILED.value and doc["attempts"] == MAX_BATCH_ATTEMPTS
    assert doc["messages"] == [{"role": "user"}], "the conversation window is kept"
    failed = [r for r in caplog.records if "CONSOLIDATION_FAILED" in r.getMessage()]
    assert len(failed) == 1


async def test_the_sweep_revivals_bound_the_whole_life_of_a_crashing_batch(world):
    await _put(world.queue)

    for _sweep in range(MAX_FAILED_REVIVALS + 3):        # more hourly sweeps than the cap allows
        await world.service.revive_failed_batches(USER)
        for _ in range(MAX_BATCH_ATTEMPTS + 2):
            await _crashing_run(world)

    assert len(world.coordinator.routed) == MAX_BATCH_ATTEMPTS * (MAX_FAILED_REVIVALS + 1)
    doc = await _doc(world.queue)
    assert doc["status"] == BatchStatus.FAILED.value and doc["revivals"] == MAX_FAILED_REVIVALS


async def test_a_batch_that_crashed_once_still_succeeds_the_next_time(world):
    await _put(world.queue)
    await _crashing_run(world)
    world.coordinator.crash = False

    await world.service.process_user_batches(USER, max_batches=1)

    assert world.coordinator.routed == ["b1", "b1"]
    assert await _doc(world.queue) is None
