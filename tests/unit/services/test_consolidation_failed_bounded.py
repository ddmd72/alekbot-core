"""A failing consolidation batch costs a bounded number of paid attempts and never starves newer batches.

Real ConsolidationService over the real FirestoreConsolidationQueue on the evaluating fake (H1); only the
agent coordinator (the LLM boundary) is scripted.
Decision: docs/04_solution_strategy/decisions/consolidation_failed_batch_bounded.md
"""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentResponse
from src.domain.consolidation import MAX_FAILED_REVIVALS, BatchStatus, ConsolidationBatch
from src.domain.user import UserBotConfig
from src.services.consolidation_service import ConsolidationService
from tests.support.account_owners import owner_zones
from tests.support.firestore_eval import EvalDb

USER = "user-abcdef123"


@pytest.fixture
def queue():
    env = MagicMock()
    env.consolidation_queue_collection = "development_consolidation_queue"
    return FirestoreConsolidationQueue(EvalDb(), env)


class _Coordinator:
    """Scripted LLM boundary: batches in `failing` fail, the rest succeed. Records the batch ids it was given."""

    def __init__(self):
        self.failing: set[str] = set()
        self.routed: list[str] = []

    async def route_message(self, message):
        self.routed.append(message.payload["batch_id"])
        if message.payload["batch_id"] in self.failing:
            return AgentResponse.failure(error="LLM 529", agent_id="c", task_id="t")
        return AgentResponse.success(result={}, agent_id="c", task_id="t")


@pytest.fixture
def coordinator():
    return _Coordinator()


@pytest.fixture
def service(queue, coordinator):
    factory = MagicMock()
    factory.ensure_agents_for_user = AsyncMock()
    # The real memory-write lease on the evaluating fake (FACT_LIFECYCLE_RFC D9): each task takes and releases it.
    lease = FirestoreMemoryWriteLease(EvalDb(), EnvironmentConfig())
    profiles = _Profiles()
    return ConsolidationService(queue=queue, coordinator=coordinator, agent_factory=factory,
                                lease=lease, run_timeout_s=900.0, user_repo=profiles,
                                owner_zones=owner_zones({"acc-1": USER}, profiles))


class _Profiles:
    async def get_user(self, user_id):
        return SimpleNamespace(account_id="acc-1", config=UserBotConfig())


async def _put(queue, batch_id, created_at, status=BatchStatus.PENDING, **extra):
    await queue.enqueue_batch(ConsolidationBatch(
        batch_id=batch_id, user_id=USER, session_id="s", messages=[{"role": "user"}],
        status=status, created_at=created_at, **extra,
    ))


async def _doc(queue, batch_id):
    return (await queue.collection.document(batch_id).get()).to_dict()


async def _task(service) -> bool:
    """One consolidation Cloud Task, as the worker runs it."""
    return await service.process_user_batches(user_id=USER, max_batches=1)


async def test_per_task_run_does_not_revive_failed_and_processes_the_newer_batch(service, queue, coordinator):
    await _put(queue, "old", 1.0, BatchStatus.FAILED, attempts=3, last_error="x")
    await _put(queue, "new", 2.0)

    has_more = await _task(service)

    assert coordinator.routed == ["new"]
    assert has_more is False
    old = await _doc(queue, "old")
    assert old["status"] == BatchStatus.FAILED.value and old["revivals"] == 0
    assert await _doc(queue, "new") is None   # consolidated and deleted


async def test_per_task_run_still_revives_a_zombie(service, queue, coordinator):
    await _put(queue, "zombie", 1.0, BatchStatus.PROCESSING)   # no processing_started_at: legacy zombie

    await _task(service)

    assert coordinator.routed == ["zombie"]


async def test_sweep_revival_makes_the_failed_batch_run_again(service, queue, coordinator):
    await _put(queue, "old", 1.0, BatchStatus.FAILED, attempts=3, last_error="x")

    assert await service.revive_failed_batches(USER) == 1
    await _task(service)

    assert coordinator.routed == ["old"]
    assert await _doc(queue, "old") is None


async def test_a_batch_that_always_fails_costs_at_most_twelve_attempts(service, queue, coordinator, caplog):
    coordinator.failing = {"bad"}
    await _put(queue, "bad", 1.0)

    with caplog.at_level(logging.ERROR):
        for _sweep in range(MAX_FAILED_REVIVALS + 3):     # more hourly sweeps than the cap allows
            await service.revive_failed_batches(USER)
            for _task_run in range(10):                   # the has_more chain of one sweep, bounded
                if not await _task(service):
                    break

    assert coordinator.routed.count("bad") == 3 * (MAX_FAILED_REVIVALS + 1)
    doc = await _doc(queue, "bad")
    assert doc["status"] == BatchStatus.FAILED.value
    assert doc["revivals"] == MAX_FAILED_REVIVALS and doc["messages"] == [{"role": "user"}]
    gave_up = [r.getMessage() for r in caplog.records if "CONSOLIDATION_GAVE_UP" in r.getMessage()]
    assert len(gave_up) == 1
    assert "batch_id=bad" in gave_up[0] and f"user_id={USER[:8]} " in gave_up[0] and f"revivals={MAX_FAILED_REVIVALS}" in gave_up[0]
    assert USER not in gave_up[0]


async def test_a_newer_batch_is_not_starved_by_a_failed_one(service, queue, coordinator):
    coordinator.failing = {"bad"}
    await _put(queue, "bad", 1.0)
    await _put(queue, "good", 2.0)

    for _ in range(3):                      # three attempts on the oldest, as the re-enqueue chain does
        await _task(service)
    assert (await _doc(queue, "bad"))["status"] == BatchStatus.FAILED.value

    for _ in range(3):                      # further tasks (overflow, re-enqueue) reach the newer batch
        await _task(service)
    assert await _doc(queue, "good") is None
    assert coordinator.routed.count("bad") == 3


async def test_no_gave_up_line_while_revivals_remain(service, queue, coordinator, caplog):
    coordinator.failing = {"bad"}
    await _put(queue, "bad", 1.0)

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            await _task(service)

    messages = [r.getMessage() for r in caplog.records]
    assert any("CONSOLIDATION_FAILED" in m for m in messages)
    assert not any("CONSOLIDATION_GAVE_UP" in m for m in messages)
