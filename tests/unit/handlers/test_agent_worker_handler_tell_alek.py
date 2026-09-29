"""tell_alek through the Cloud Task worker (VOICE_COMPANION_RFC §4.15.2).

On success the gateway has already posted the outcome to chat, so the worker adds nothing. On
failure — a failed response or an exception — the worker tells the user the errand did not
complete: nobody waits for it on the line, so a silent failure would be a lost errand.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentResponse
from src.domain.notification_kind import NotificationKind
from src.handlers.agent_worker_handler import AgentWorkerHandler

_CONTEXT = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "D9", "origin_platform": "slack"}
_PAYLOAD = {"task_type": "agent_execution", "agent_id": "alek_agent", "intent": "tell_alek",
            "query": "[Sep 28, 10:20 UTC] Set a reminder to leave for the airport at 13:30",
            "context": _CONTEXT}


def _handler(route_result=None, route_error=None):
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock(return_value=route_result, side_effect=route_error)
    notification = AsyncMock()
    handler = AgentWorkerHandler(coordinator=coordinator, notification_service=notification,
                                 task_queue=AsyncMock(), doc_delivery_service=None)
    return handler, coordinator, notification


@pytest.mark.asyncio
async def test_success_adds_nothing_because_the_gateway_already_posted():
    handler, _, notification = _handler(AgentResponse.success(task_id="t", agent_id="alek_agent_u1", result="done"))

    result = await handler.handle_task(_PAYLOAD)

    assert result["status"] == "success"
    notification.notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_errand_is_reported_to_the_origin_channel():
    handler, _, notification = _handler(AgentResponse.failure(task_id="t", agent_id="alek_agent_u1",
                                                              error="Alek did not answer: timeout"))

    result = await handler.handle_task(_PAYLOAD)

    assert result["status"] == "failed"
    notification.notify.assert_awaited_once()
    kwargs = notification.notify.await_args.kwargs
    assert kwargs["kind"] is NotificationKind.DOCUMENT_DELIVERY
    assert kwargs["user_id"] == "u1" and kwargs["channel_id_override"] == "D9"
    assert "Set a reminder to leave for the airport" in kwargs["system_alert"]
    assert "timeout" not in kwargs["system_alert"]  # never the raw error text


@pytest.mark.asyncio
async def test_an_exception_is_reported_too_and_re_raised():
    handler, _, notification = _handler(route_error=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await handler.handle_task(_PAYLOAD)

    notification.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failing_notice_does_not_mask_the_result():
    handler, _, notification = _handler(AgentResponse.failure(task_id="t", agent_id="a", error="x"))
    notification.notify.side_effect = RuntimeError("slack down")

    result = await handler.handle_task(_PAYLOAD)

    assert result["status"] == "failed"
