import pytest
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentResponse, AgentStatus, AgentIntent
from src.domain.messaging import MessageContext, SmartResponse
from src.services.smart_retry_service import SmartRetryService


def _ctx():
    return MessageContext(text="hi", session_id="user-1:C0123456", user_id="user-1234", account_id="a1", thread_id=None)


class TestSchedule:
    @pytest.mark.asyncio
    async def test_enqueues_with_task_type_and_session_dedup_key(self):
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(return_value="task-1")
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=MagicMock(), notification=MagicMock())

        await svc.schedule(_ctx(), [])

        task_dispatch.enqueue_worker_task.assert_awaited_once()
        kwargs = task_dispatch.enqueue_worker_task.await_args.kwargs
        assert kwargs["task_type"] == SmartRetryService.TASK_TYPE
        assert kwargs["dedup_key"] == "user-1:C0123456"
        assert kwargs["payload"]["user_id"] == "user-1234"
        assert kwargs["payload"]["session_id"] == "user-1:C0123456"
        assert kwargs["payload"]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_no_task_dispatch_configured_is_a_noop(self):
        svc = SmartRetryService(task_dispatch=None, coordinator=MagicMock(), notification=MagicMock())

        await svc.schedule(_ctx(), [])  # must not raise

    @pytest.mark.asyncio
    async def test_scheduling_failure_never_raises(self):
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(side_effect=RuntimeError("queue down"))
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=MagicMock(), notification=MagicMock())

        await svc.schedule(_ctx(), [])  # must not raise — best-effort


class TestExecute:
    @pytest.mark.asyncio
    async def test_delivers_follow_up_on_success(self):
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text="я тут крепко подумал — вот ответ", structured_data=None, link_list=[]),
            )
        )
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        result, status = await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "user-1:C0123456",
            "thread_id": None, "text": "original question", "message_parts": [{"text": "original question"}],
        })

        assert status == 200
        notification.notify_text.assert_awaited_once()
        kwargs = notification.notify_text.await_args.kwargs
        assert kwargs["user_id"] == "user-1"
        assert kwargs["text"] == "я тут крепко подумал — вот ответ"
        assert kwargs["session_id"] == "user-1:C0123456"

    @pytest.mark.asyncio
    async def test_dispatches_to_smart_directly_bypassing_router(self):
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text="ok", structured_data=None, link_list=[]),
            )
        )
        # notify_text must be an AsyncMock, not the default MagicMock attribute —
        # this test's retry succeeds with non-empty text, so execute() awaits it.
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        sent_message = coordinator.route_message.await_args.args[0]
        assert sent_message.recipient == "smart_response_agent_user-1"
        assert sent_message.intent == AgentIntent.QUERY
        assert sent_message.payload["text"] == "q"

    @pytest.mark.asyncio
    async def test_silently_drops_on_second_failure(self):
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.timeout(task_id="t", agent_id="smart_response_agent_u", error="timed out again")
        )
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        result, status = await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        assert status == 200  # never a 5xx — must not trigger Cloud Tasks retry
        notification.notify_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_route_message_exception_still_returns_200(self):
        """The agent-tasks queue retries a non-2xx response up to 3 times
        (retry_config.max_attempts=3) — an uncaught exception here must never
        propagate into a non-200, or one timeout becomes up to 4 Smart runs."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(side_effect=RuntimeError("boom"))
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=MagicMock())

        result, status = await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        assert status == 200

    @pytest.mark.asyncio
    async def test_missing_required_fields_returns_200(self):
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=MagicMock(), notification=MagicMock())

        result, status = await svc.execute({})

        assert status == 200
