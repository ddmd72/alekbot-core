"""AgentWorkerHandler — a redelivered Cloud Task is not executed twice.

Incident 2026-10-09: one create_html_page task (single enqueue by the research job) was
delivered twice, 208 s apart, while the first attempt was still running → two reports.
Cloud Tasks is at-least-once; see docs/04_solution_strategy/decisions/worker_task_dedup.md.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.ports.dedup_store import DedupStore

_PAYLOAD = {
    "task_type": "agent_execution",
    "agent_id": "html_page_generator_agent",
    "intent": "generic",
    "query": "q",
    "context": {"user_id": "u1", "account_id": "a1"},
}
_DELIVERY = CloudTaskDelivery(task_name="0038899772573220363", retry_count=0, execution_count=0)


def _handler(dedup=None):
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result=None)
    )
    return AgentWorkerHandler(coordinator=coordinator, task_dedup=dedup), coordinator


def _dedup(claimed=True):
    store = AsyncMock(spec=DedupStore)
    store.try_mark_processed = AsyncMock(return_value=claimed)
    return store


async def test_first_delivery_claims_by_task_name_and_runs():
    store = _dedup(claimed=True)
    handler, coordinator = _handler(store)

    result = await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)

    store.try_mark_processed.assert_awaited_once_with("0038899772573220363")
    coordinator.route_message.assert_awaited_once()
    assert result["status"] == "success"


async def test_redelivery_of_claimed_task_is_skipped():
    store = _dedup(claimed=False)
    handler, coordinator = _handler(store)

    result = await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)

    coordinator.route_message.assert_not_awaited()
    assert result["status"] == "duplicate"
    store.release.assert_not_awaited()


async def test_raising_attempt_releases_claim_so_the_retry_runs():
    """An exception becomes a 500 and Cloud Tasks retries — that retry must not be skipped."""
    store = _dedup(claimed=True)
    handler, coordinator = _handler(store)
    coordinator.route_message = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)

    store.release.assert_awaited_once_with("0038899772573220363")


async def test_failed_agent_response_keeps_claim():
    """A FAILED response returns 200 and the user is notified — Cloud Tasks does not retry,
    so a later delivery is a duplicate and must stay skipped."""
    store = _dedup(claimed=True)
    handler, coordinator = _handler(store)
    coordinator.route_message = AsyncMock(return_value=AgentResponse.failure(
        task_id="t", agent_id="a", error="provider error"))

    result = await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)

    assert result["status"] == "failed"
    store.release.assert_not_awaited()


async def test_claim_store_error_fails_open():
    """A duplicate beats a lost task: a broken store must not block execution."""
    store = _dedup()
    store.try_mark_processed = AsyncMock(side_effect=RuntimeError("firestore down"))
    handler, coordinator = _handler(store)

    result = await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)

    coordinator.route_message.assert_awaited_once()
    assert result["status"] == "success"


async def test_release_error_still_reraises_original():
    store = _dedup(claimed=True)
    store.release = AsyncMock(side_effect=RuntimeError("firestore down"))
    handler, coordinator = _handler(store)
    coordinator.route_message = AsyncMock(side_effect=ValueError("agent boom"))

    with pytest.raises(ValueError, match="agent boom"):
        await handler.handle_task(_PAYLOAD, delivery=_DELIVERY)


@pytest.mark.parametrize("delivery,with_store", [(None, True), (_DELIVERY, False)])
async def test_no_delivery_or_no_store_runs_without_claim(delivery, with_store):
    store = _dedup() if with_store else None
    handler, coordinator = _handler(store)

    await handler.handle_task(_PAYLOAD, delivery=delivery)

    coordinator.route_message.assert_awaited_once()
    if store:
        store.try_mark_processed.assert_not_awaited()
