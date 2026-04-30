"""
Session-per-channel tests.

Validates that session IDs follow the `user_id:channel_id` format across all
platform adapters, notification services, delegation engine context passthrough,
worker handlers, and the GCP task queue serialiser.

Key invariants:
  - Session ID = f"{user_id}:{channel_id}" — deterministic, sync, no Firestore lookup.
  - DM channels (D...) are used as-is — no special casing.
  - origin_channel_id flows through delegation context to coordinator and back.
  - NotificationService derives session from channel when session_id=None.
  - WorkerHandler extracts channel from session_id format "user:channel".
  - _DomainEncoder handles Pydantic BaseModel serialization.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from src.adapters.gcp_task_queue import _DomainEncoder
from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.domain.llm import Message, MessagePart
from src.domain.messaging import SmartResponse
from src.domain.notification import NotificationChannel
from src.infrastructure.delegation_engine import DelegationEngine, DelegationResult
from src.ports.llm_port import LLMResponse, ToolCall
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.ports.session_store import SessionStore
from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import NOTIFICATION_SLA
from src.services.user_notification_service import UserNotificationService


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER = "U_user123"
_ACCOUNT = "acc_test"
_SLACK_CHANNEL = "C_general"
_SLACK_DM = "D_dm_abc"
_TELEGRAM_CHAT = "987654321"


# =========================================================================
# 1. Slack adapter — _resolve_session_id
# =========================================================================

class TestSlackResolveSessionId:
    """Slack HTTPModeAdapter._resolve_session_id — sync, deterministic."""

    def _make_adapter(self):
        """Minimal adapter instance — only _resolve_session_id is tested (no Slack app needed)."""
        from src.adapters.slack.http_adapter import HTTPModeAdapter
        # The method is a plain sync method on the class; we can call it
        # without a fully wired adapter by using __new__ to skip __init__.
        adapter = object.__new__(HTTPModeAdapter)
        return adapter

    def test_regular_channel(self):
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, _SLACK_CHANNEL)
        assert result == f"{_USER}:{_SLACK_CHANNEL}"

    def test_dm_channel_used_as_is(self):
        """DM channels (D...) are NOT replaced — no special casing."""
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, _SLACK_DM)
        assert result == f"{_USER}:{_SLACK_DM}"

    def test_format_contains_colon_separator(self):
        adapter = self._make_adapter()
        result = adapter._resolve_session_id("U_abc", "C_xyz")
        assert ":" in result
        user_part, channel_part = result.split(":", 1)
        assert user_part == "U_abc"
        assert channel_part == "C_xyz"

    def test_is_sync_not_coroutine(self):
        """_resolve_session_id must be sync (no Firestore lookup)."""
        import asyncio
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, _SLACK_CHANNEL)
        assert not asyncio.iscoroutine(result)


# =========================================================================
# 2. Telegram adapter — _resolve_session_id
# =========================================================================

class TestTelegramResolveSessionId:
    """TelegramWebhookAdapter._resolve_session_id — sync, deterministic."""

    def _make_adapter(self):
        from src.adapters.telegram.webhook_adapter import TelegramWebhookAdapter
        adapter = object.__new__(TelegramWebhookAdapter)
        return adapter

    def test_regular_chat(self):
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, _TELEGRAM_CHAT)
        assert result == f"{_USER}:{_TELEGRAM_CHAT}"

    def test_negative_chat_id(self):
        """Telegram group chats have negative IDs — must be preserved."""
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, "-100123456")
        assert result == f"{_USER}:-100123456"

    def test_format_matches_slack(self):
        """Both adapters produce the same user:channel format."""
        from src.adapters.slack.http_adapter import HTTPModeAdapter
        slack = object.__new__(HTTPModeAdapter)
        tg = self._make_adapter()
        assert slack._resolve_session_id("U1", "C1") == "U1:C1"
        assert tg._resolve_session_id("U1", "C1") == "U1:C1"

    def test_is_sync_not_coroutine(self):
        import asyncio
        adapter = self._make_adapter()
        result = adapter._resolve_session_id(_USER, _TELEGRAM_CHAT)
        assert not asyncio.iscoroutine(result)


# =========================================================================
# 3. NotificationService.notify — session derivation
# =========================================================================

class TestNotificationServiceNotifySession:
    """notify() derives session_id = f"{user_id}:{channel_id}" when session_id is None."""

    @pytest.fixture
    def _deps(self):
        state_repo = AsyncMock(spec=NotificationStatePort)
        channel_factory = MagicMock(spec=NotificationChannelFactoryPort)
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock(return_value=AgentResponse.success(
            task_id="t1", agent_id="quick", result=SmartResponse(text="OK"),
        ))
        session_store = AsyncMock(spec=SessionStore)

        response_channel = AsyncMock()
        response_channel.max_message_length = 4000
        response_channel.send_message = AsyncMock()
        channel_factory.create.return_value = response_channel

        service = UserNotificationService(
            state_repo=state_repo,
            channel_factory=channel_factory,
            coordinator=coordinator,
            notification_sla=NOTIFICATION_SLA,
            session_store=session_store,
        )
        return service, state_repo, coordinator, session_store

    async def test_session_derived_from_channel_when_none(self, _deps):
        service, state_repo, coordinator, _ = _deps
        state_repo.get_primary.return_value = NotificationChannel(
            user_id=_USER, platform="slack", channel_id=_SLACK_CHANNEL,
            updated_at=datetime.now(timezone.utc),
        )

        await service.notify(
            kind=NotificationKind.INTERACTIVE,
            user_id=_USER,
            account_id=_ACCOUNT,
            system_alert="Test alert",
            session_id=None,
        )

        # Verify the AgentMessage was created with derived session_id
        coordinator.route_message.assert_called_once()
        msg: AgentMessage = coordinator.route_message.call_args[0][0]
        assert msg.context["session_id"] == f"{_USER}:{_SLACK_CHANNEL}"

    async def test_explicit_session_id_preserved(self, _deps):
        service, state_repo, coordinator, _ = _deps
        state_repo.get_primary.return_value = NotificationChannel(
            user_id=_USER, platform="slack", channel_id=_SLACK_CHANNEL,
            updated_at=datetime.now(timezone.utc),
        )

        await service.notify(
            kind=NotificationKind.INTERACTIVE,
            user_id=_USER,
            account_id=_ACCOUNT,
            system_alert="Test alert",
            session_id="explicit-session-id",
        )

        msg: AgentMessage = coordinator.route_message.call_args[0][0]
        assert msg.context["session_id"] == "explicit-session-id"

    async def test_dm_channel_no_special_casing(self, _deps):
        """DM channels (D...) flow through without being replaced by U..."""
        service, state_repo, coordinator, _ = _deps
        state_repo.get_primary.return_value = NotificationChannel(
            user_id=_USER, platform="slack", channel_id=_SLACK_DM,
            updated_at=datetime.now(timezone.utc),
        )

        await service.notify(
            kind=NotificationKind.INTERACTIVE,
            user_id=_USER,
            account_id=_ACCOUNT,
            system_alert="Test alert",
            session_id=None,
        )

        msg: AgentMessage = coordinator.route_message.call_args[0][0]
        assert msg.context["session_id"] == f"{_USER}:{_SLACK_DM}"


# =========================================================================
# 4. NotificationService.notify_document_link — session derivation
# =========================================================================

class TestNotificationServiceDocLink:
    """notify_document_link uses f"{user_id}:{channel_id}" for history session."""

    @pytest.fixture
    def _deps(self):
        state_repo = AsyncMock(spec=NotificationStatePort)
        channel_factory = MagicMock(spec=NotificationChannelFactoryPort)
        coordinator = MagicMock()
        session_store = AsyncMock(spec=SessionStore)

        response_channel = AsyncMock()
        response_channel.send_document_link = AsyncMock()
        channel_factory.create.return_value = response_channel

        service = UserNotificationService(
            state_repo=state_repo,
            channel_factory=channel_factory,
            coordinator=coordinator,
            notification_sla=NOTIFICATION_SLA,
            session_store=session_store,
        )
        return service, state_repo, session_store

    async def test_doc_session_id_uses_channel(self, _deps):
        service, state_repo, session_store = _deps
        state_repo.get_primary.return_value = NotificationChannel(
            user_id=_USER, platform="slack", channel_id=_SLACK_CHANNEL,
            updated_at=datetime.now(timezone.utc),
        )

        await service.notify_document_link(
            user_id=_USER,
            account_id=_ACCOUNT,
            url="https://example.com/doc.html",
            label="Report",
        )

        session_store.append_messages_batch.assert_called_once()
        call_kwargs = session_store.append_messages_batch.call_args[1]
        assert call_kwargs["session_id"] == f"{_USER}:{_SLACK_CHANNEL}"

    async def test_doc_session_id_dm_channel(self, _deps):
        """DM channel ID flows directly into session_id."""
        service, state_repo, session_store = _deps
        state_repo.get_primary.return_value = NotificationChannel(
            user_id=_USER, platform="slack", channel_id=_SLACK_DM,
            updated_at=datetime.now(timezone.utc),
        )

        await service.notify_document_link(
            user_id=_USER,
            account_id=_ACCOUNT,
            url="https://example.com/doc.html",
            label="Report",
        )

        call_kwargs = session_store.append_messages_batch.call_args[1]
        assert call_kwargs["session_id"] == f"{_USER}:{_SLACK_DM}"


# =========================================================================
# 5. DelegationEngine — context passthrough
# =========================================================================

class TestDelegationEngineContextPassthrough:
    """DelegationEngine.execute passes context dict (including origin fields) to coordinator."""

    @pytest.fixture
    def _deps(self):
        coordinator = MagicMock()
        coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
            task_id="t1", agent_id="memory", result="facts found",
        ))
        engine = DelegationEngine(coordinator=coordinator)
        return engine, coordinator

    async def test_origin_channel_id_flows_to_coordinator(self, _deps):
        engine, coordinator = _deps

        context = {
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "session_id": f"{_USER}:{_SLACK_CHANNEL}",
            "origin_channel_id": _SLACK_CHANNEL,
            "origin_platform": "slack",
        }

        # LLM returns a single tool call, then text on second turn
        call_count = 0

        async def mock_call_llm(request, turn):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResponse(
                    text="",
                    tool_calls=[ToolCall(
                        id="tc1",
                        name="delegate_to_specialist",
                        args={"intent": "search_memory", "query": "test"},
                    )],
                )
            return LLMResponse(text="Memory result", tool_calls=[])

        base_request = MagicMock()
        base_request.messages = [Message(role="user", parts=[MessagePart(text="test")])]
        base_request.model_copy = MagicMock(side_effect=lambda update: MagicMock(messages=update["messages"]))

        await engine.execute(
            call_llm=mock_call_llm,
            base_request=base_request,
            context=context,
            max_turns=3,
        )

        # Verify coordinator received the context with origin fields
        coordinator.handle_delegation.assert_called_once()
        call_kwargs = coordinator.handle_delegation.call_args[1]
        delegation_ctx = call_kwargs["context"]
        assert delegation_ctx["origin_channel_id"] == _SLACK_CHANNEL
        assert delegation_ctx["origin_platform"] == "slack"
        assert delegation_ctx["user_id"] == _USER

    async def test_context_is_plain_dict_not_dataclass(self, _deps):
        """DelegationContext dataclass was removed — context is a plain Dict[str, Any]."""
        engine, coordinator = _deps

        context = {"user_id": _USER, "custom_field": "value123"}

        async def mock_call_llm(request, turn):
            return LLMResponse(
                text="",
                tool_calls=[ToolCall(
                    id="tc1",
                    name="delegate_to_specialist",
                    args={"intent": "search_memory", "query": "q"},
                )],
            )

        base_request = MagicMock()
        base_request.messages = [Message(role="user", parts=[MessagePart(text="test")])]
        base_request.model_copy = MagicMock(side_effect=lambda update: MagicMock(messages=update["messages"]))

        await engine.execute(
            call_llm=mock_call_llm,
            base_request=base_request,
            context=context,
            max_turns=1,
        )

        call_kwargs = coordinator.handle_delegation.call_args[1]
        delegation_ctx = call_kwargs["context"]
        # Custom fields from message.context spread through
        assert delegation_ctx["custom_field"] == "value123"
        assert delegation_ctx["user_id"] == _USER


# =========================================================================
# 6. _DomainEncoder — Pydantic BaseModel serialization
# =========================================================================

class TestDomainEncoder:
    """GcpTaskQueue._DomainEncoder handles Pydantic models via model_dump()."""

    def test_pydantic_model_serialized(self):
        class SampleModel(BaseModel):
            name: str
            value: int

        obj = SampleModel(name="test", value=42)
        result = json.dumps({"data": obj}, cls=_DomainEncoder)
        parsed = json.loads(result)
        assert parsed["data"] == {"name": "test", "value": 42}

    def test_nested_pydantic_model(self):
        class Inner(BaseModel):
            x: int

        class Outer(BaseModel):
            inner: Inner
            label: str

        obj = Outer(inner=Inner(x=10), label="outer")
        result = json.dumps(obj, cls=_DomainEncoder)
        parsed = json.loads(result)
        assert parsed == {"inner": {"x": 10}, "label": "outer"}

    def test_non_pydantic_falls_through(self):
        """Non-Pydantic objects should raise TypeError (via super().default)."""
        class Custom:
            pass

        with pytest.raises(TypeError):
            json.dumps(Custom(), cls=_DomainEncoder)

    def test_native_types_unaffected(self):
        payload = {"str": "hello", "int": 42, "list": [1, 2], "none": None}
        result = json.dumps(payload, cls=_DomainEncoder)
        assert json.loads(result) == payload

    def test_message_part_pydantic(self):
        """MessagePart is Pydantic — should serialize via model_dump."""
        part = MessagePart(text="hello world")
        result = json.dumps({"part": part}, cls=_DomainEncoder)
        parsed = json.loads(result)
        assert parsed["part"]["text"] == "hello world"


# =========================================================================
# 7. AgentWorkerHandler — origin context extraction
# =========================================================================

class TestAgentWorkerHandlerOriginContext:
    """AgentWorkerHandler passes origin_channel_id/origin_platform from context to notify calls."""

    def _make_handler(self):
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        notification = AsyncMock()
        notification.notify = AsyncMock()
        notification.notify_file_bytes = AsyncMock()
        notification.notify_document_link = AsyncMock()
        doc_delivery = AsyncMock()
        doc_delivery.store = AsyncMock(return_value="https://example.com/doc.pdf")
        task_queue = AsyncMock()

        from src.handlers.agent_worker_handler import AgentWorkerHandler
        handler = AgentWorkerHandler(
            coordinator=coordinator,
            notification_service=notification,
            task_queue=task_queue,
            doc_delivery_service=doc_delivery,
        )
        return handler, coordinator, notification, doc_delivery

    async def test_deep_research_passes_origin_channel(self):
        handler, coordinator, notification, _ = self._make_handler()

        coordinator.route_message.return_value = AgentResponse.success(
            task_id="t1",
            agent_id="claude_deep_research_runner",
            result={"text": "Research findings", "query": "AI"},
        )

        context = {
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "session_id": f"{_USER}:{_SLACK_CHANNEL}",
            "origin_channel_id": _SLACK_CHANNEL,
            "origin_platform": "slack",
        }

        with patch("src.handlers.agent_worker_handler.deliver_deep_research", new_callable=AsyncMock) as mock_deliver:
            from src.infrastructure.agent_manifest import Intent
            await handler.handle_task({
                "task_type": "agent_execution",
                "agent_id": "claude_deep_research_runner",
                "intent": Intent.EXECUTE_DEEP_RESEARCH_CLAUDE,
                "query": "Research AI",
                "context": context,
            })

            mock_deliver.assert_called_once()
            call_kwargs = mock_deliver.call_args[1]
            assert call_kwargs["channel_id_override"] == _SLACK_CHANNEL
            assert call_kwargs["platform_override"] == "slack"

    async def test_document_delivery_passes_origin_channel(self):
        handler, coordinator, notification, doc_delivery = self._make_handler()

        import base64
        content_b64 = base64.b64encode(b"<html>test</html>").decode()
        coordinator.route_message.return_value = AgentResponse.success(
            task_id="t1",
            agent_id="pdf_generator",
            result="done",
            delivery_items=[
                MagicMock(type="document", data={
                    "content_b64": content_b64,
                    "filename": "report.pdf",
                    "content_type": "application/pdf",
                    "label": "Report",
                }),
            ],
        )

        context = {
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "origin_channel_id": _SLACK_CHANNEL,
            "origin_platform": "slack",
        }

        from src.infrastructure.agent_manifest import Intent
        await handler.handle_task({
            "task_type": "agent_execution",
            "agent_id": "pdf_generator",
            "intent": Intent.CREATE_PDF,
            "query": "Create a PDF",
            "context": context,
        })

        notification.notify_document_link.assert_called_once()
        call_kwargs = notification.notify_document_link.call_args[1]
        assert call_kwargs["channel_id_override"] == _SLACK_CHANNEL
        assert call_kwargs["platform_override"] == "slack"

    async def test_failure_notification_passes_origin_channel(self):
        handler, coordinator, notification, _ = self._make_handler()

        coordinator.route_message.return_value = AgentResponse(
            task_id="t1",
            agent_id="pdf_generator",
            status=AgentStatus.FAILED,
            result=None,
            confidence=0.0,
            error="generation failed",
        )

        context = {
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "origin_channel_id": _SLACK_CHANNEL,
            "origin_platform": "slack",
        }

        from src.infrastructure.agent_manifest import Intent
        await handler.handle_task({
            "task_type": "agent_execution",
            "agent_id": "pdf_generator",
            "intent": Intent.CREATE_PDF,
            "query": "Create a PDF",
            "context": context,
        })

        notification.notify.assert_called_once()
        call_kwargs = notification.notify.call_args[1]
        assert call_kwargs["channel_id_override"] == _SLACK_CHANNEL
        assert call_kwargs["platform_override"] == "slack"


# =========================================================================
# 8. WorkerHandler — session_id channel extraction
# =========================================================================

class TestWorkerHandlerSessionExtraction:
    """WorkerHandler extracts origin_channel_id from session_id format 'user:channel'."""

    def _make_worker(self):
        agent_worker = AsyncMock()
        agent_worker.handle_task = AsyncMock(return_value={"status": "success"})

        notification = AsyncMock()
        notification.notify = AsyncMock()

        from src.handlers.worker_handler import WorkerHandler

        worker = WorkerHandler(
            agent_worker_handler=agent_worker,
            email_indexing_service=MagicMock(),
            notification_service=notification,
            consolidation_service=None,
            coordinator=MagicMock(),
            agent_factory=AsyncMock(),
            indexed_email_repo=None,
            user_repo=MagicMock(),
        )
        return worker, notification

    async def test_deep_research_polling_extracts_channel_from_session_id(self):
        worker, notification = self._make_worker()

        session_id = f"{_USER}:{_SLACK_CHANNEL}"

        # Set up job_registry and task_dispatch for polling
        mock_job_port = AsyncMock()
        # Return "completed" status
        mock_job_port.get_status = AsyncMock(return_value=("completed", "Research result text"))

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_job_port
        worker._job_registry = mock_registry

        mock_task_dispatch = AsyncMock()
        worker._task_dispatch = mock_task_dispatch

        mock_agent_factory = AsyncMock()
        worker._agent_factory = mock_agent_factory

        payload = {
            "task_type": "deep_research_polling",
            "interaction_id": "int-001",
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "session_id": session_id,
            "attempt": 1,
            "consecutive_errors": 0,
            "provider": "gemini",
            "query": "AI research",
        }

        with patch("src.handlers.worker_handler.deliver_deep_research", new_callable=AsyncMock) as mock_deliver:
            await worker.handle(payload)

            mock_deliver.assert_called_once()
            call_kwargs = mock_deliver.call_args[1]
            assert call_kwargs["channel_id_override"] == _SLACK_CHANNEL

    async def test_session_id_without_colon_yields_no_channel(self):
        """Legacy session_id (no colon) yields origin_channel_id=None."""
        worker, notification = self._make_worker()

        mock_job_port = AsyncMock()
        mock_job_port.get_status = AsyncMock(return_value=("timeout", None))

        mock_registry = MagicMock()
        mock_registry.get.return_value = mock_job_port
        worker._job_registry = mock_registry

        mock_task_dispatch = AsyncMock()
        worker._task_dispatch = mock_task_dispatch

        mock_agent_factory = AsyncMock()
        worker._agent_factory = mock_agent_factory

        payload = {
            "task_type": "deep_research_polling",
            "interaction_id": "int-002",
            "user_id": _USER,
            "account_id": _ACCOUNT,
            "session_id": _USER,  # legacy: no colon
            "attempt": 100,
            "consecutive_errors": 0,
            "provider": "gemini",
        }

        await worker.handle(payload)

        notification.notify.assert_called_once()
        call_kwargs = notification.notify.call_args[1]
        assert call_kwargs.get("channel_id_override") is None


# =========================================================================
# 9. Cross-cutting: session format consistency
# =========================================================================

class TestSessionFormatConsistency:
    """Verify that session_id.split(':', 1) round-trips correctly for all adapters."""

    @pytest.mark.parametrize("user_id,channel_id", [
        ("U_abc", "C_general"),
        ("U_abc", "D_dm_xyz"),
        ("U_abc", "-100123456"),
        ("f1d66955-cb00-4d2b-8044-4eeff781b7f4", "C_channel"),
        ("user_123", "12345"),
    ])
    def test_split_roundtrip(self, user_id, channel_id):
        session_id = f"{user_id}:{channel_id}"
        parts = session_id.split(":", 1)
        assert parts[0] == user_id
        assert parts[1] == channel_id

    def test_colon_in_channel_id_safe(self):
        """Channel IDs should not contain colons in practice, but split(1) handles it."""
        session_id = "U_user:C_chan:extra"
        parts = session_id.split(":", 1)
        assert parts[0] == "U_user"
        assert parts[1] == "C_chan:extra"
