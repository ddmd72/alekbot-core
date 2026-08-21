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

        result = await svc.schedule(_ctx(), [])

        assert result is True  # fresh enqueue → a retry is genuinely in flight
        task_dispatch.enqueue_worker_task.assert_awaited_once()
        kwargs = task_dispatch.enqueue_worker_task.await_args.kwargs
        assert kwargs["task_type"] == SmartRetryService.TASK_TYPE
        assert kwargs["dedup_key"] == "user-1:C0123456"
        assert kwargs["payload"]["user_id"] == "user-1234"
        assert kwargs["payload"]["session_id"] == "user-1:C0123456"
        assert kwargs["payload"]["text"] == "hi"

    @pytest.mark.asyncio
    async def test_includes_origin_channel_derived_from_session_id_and_passed_platform(self):
        """origin_channel_id is derived from session_id (f"{user_id}:{channel_id}"),
        NOT read off MessageContext.metadata — Slack's app_mention flow never
        populates metadata["channel"], but session_id always carries the real
        channel. origin_platform can't be derived this way (session_id doesn't
        encode platform) — the caller passes it explicitly."""
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(return_value="task-1")
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=MagicMock(), notification=MagicMock())

        await svc.schedule(_ctx(), [], origin_platform="slack")

        kwargs = task_dispatch.enqueue_worker_task.await_args.kwargs
        assert kwargs["payload"]["origin_channel_id"] == "C0123456"
        assert kwargs["payload"]["origin_platform"] == "slack"

    @pytest.mark.asyncio
    async def test_no_task_dispatch_configured_is_a_noop(self):
        svc = SmartRetryService(task_dispatch=None, coordinator=MagicMock(), notification=MagicMock())

        result = await svc.schedule(_ctx(), [])  # must not raise

        assert result is False  # nothing was scheduled — caller must not promise a follow-up

    @pytest.mark.asyncio
    async def test_scheduling_failure_never_raises(self):
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(side_effect=RuntimeError("queue down"))
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=MagicMock(), notification=MagicMock())

        result = await svc.schedule(_ctx(), [])  # must not raise — best-effort

        assert result is False

    @pytest.mark.asyncio
    async def test_dedup_noop_returns_false(self):
        """enqueue_worker_task returning None means Cloud Tasks deduped this against
        an already in-flight retry for the same session — no NEW retry started, so
        the caller must not tell the user a follow-up may arrive."""
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(return_value=None)
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=MagicMock(), notification=MagicMock())

        result = await svc.schedule(_ctx(), [])

        assert result is False


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
    async def test_delivers_to_origin_channel_not_primary(self):
        """CRITICAL for per-channel session isolation: without channel_id_override
        + platform_override, notify_text()'s _resolve_channel falls back to the
        user's primary/last-active channel, which may not be the channel the
        original timed-out request came from. origin_channel_id/origin_platform
        (carried in the payload by schedule()) must reach notify_text AND the
        retried AgentMessage's own context, so any further async delivery the
        retry triggers (e.g. document generation) also targets the right channel."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text="ok", structured_data=None, link_list=[]),
            )
        )
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "user-1:C0123456",
            "text": "q", "message_parts": [],
            "origin_channel_id": "C0123456", "origin_platform": "slack",
        })

        notify_kwargs = notification.notify_text.await_args.kwargs
        assert notify_kwargs["channel_id_override"] == "C0123456"
        assert notify_kwargs["platform_override"] == "slack"

        sent_message = coordinator.route_message.await_args.args[0]
        assert sent_message.context["origin_channel_id"] == "C0123456"
        assert sent_message.context["origin_platform"] == "slack"

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

    @pytest.mark.asyncio
    async def test_empty_text_returns_retry_empty_without_notifying(self):
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text="   ", structured_data=None, link_list=[]),
            )
        )
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        result, status = await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        assert status == 200
        assert result["status"] == "retry_empty"
        notification.notify_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_text_does_not_crash_and_is_treated_as_empty(self):
        """Regression: SmartResponse.text can be an explicit None — SmartResponse is
        a plain dataclass with no runtime validation, and smart_response_agent.py
        builds it from an args.get(...) default that only fires when the key is
        absent, so an explicit JSON null reaches it unguarded. .strip() on None
        must not crash execute(), and the retry must be dropped silently (not
        deliver a garbled string) exactly like a genuinely empty answer."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text=None, structured_data=None, link_list=[]),
            )
        )
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=MagicMock(), coordinator=coordinator, notification=notification)

        result, status = await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        assert status == 200
        assert result["status"] == "retry_empty"
        notification.notify_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_to_smart_exactly_once_and_never_schedules(self):
        """The single most important correctness property in this file: one retry
        per timeout. execute() must call route_message exactly once and must never
        itself call back into scheduling — a future regression that made execute()
        re-schedule itself (directly or by retrying route_message in a loop) would
        turn one Smart timeout into unbounded retries."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(
            return_value=AgentResponse.success(
                task_id="t", agent_id="smart_response_agent_u",
                result=SmartResponse(text="ok", structured_data=None, link_list=[]),
            )
        )
        task_dispatch = MagicMock()
        task_dispatch.enqueue_worker_task = AsyncMock(return_value="task-1")
        notification = MagicMock()
        notification.notify_text = AsyncMock()
        svc = SmartRetryService(task_dispatch=task_dispatch, coordinator=coordinator, notification=notification)

        await svc.execute({
            "user_id": "user-1", "account_id": "account-1", "session_id": "s",
            "text": "q", "message_parts": [],
        })

        coordinator.route_message.assert_awaited_once()
        task_dispatch.enqueue_worker_task.assert_not_called()
