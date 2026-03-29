"""
Unit tests for ConversationHandler utility methods and handle_command.

Coverage:
  strtobool
    - truthy strings → True
    - falsy strings  → False
    - invalid string → ValueError

  _get_consolidation_config
    - user found with config → uses user-specific values
    - user not found         → returns global config
    - exception in user_repo → returns global config (fail-safe)

  validate_model_output
    - no security_port  → pass through unchanged
    - CRITICAL risk     → returns blocked placeholder
    - HIGH risk         → returns sanitized text
    - SAFE/other risk   → returns sanitized text unchanged
    - exception in port → returns original text (fail open)

  _save_history_with_retry
    - success on first attempt → append_messages_batch called once
    - transient error then success → retries, ultimately saves
    - non-transient error → raises immediately without retry
    - all attempts exhausted → raises last exception

  handle_command
    - admin_cache_reset success         → send_message called with success text
    - admin_cache_reset exception       → send_message called with error text
    - consolidate no queue              → sends not-initialized message
    - consolidate no session messages   → sends not-enough message
    - consolidate success               → enqueues batch, calls overflow_callback
    - unknown command                   → sends unknown-command message
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentStatus
from src.domain.messaging import MessageContext
from src.domain.prompt_v3.security import RiskLevel
from src.domain.settings import ConsolidationSettings
from src.handlers.conversation_handler import ConversationHandler, strtobool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID   = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"


def _make_handler(
    *,
    with_queue: bool = True,
    overflow_callback=None,
    notification_service=None,
):
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    session_store.save_session = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)
    agent_factory.invalidate_prompt_cache = MagicMock()

    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=None)
    agent_factory.user_repo = user_repo

    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()

    consolidation_queue = MagicMock() if with_queue else None
    if consolidation_queue:
        consolidation_queue.enqueue_batch = AsyncMock(return_value="batch-id-1")

    handler = ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        consolidation_queue=consolidation_queue,
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        overflow_callback=overflow_callback,
        notification_service=notification_service,
    )
    return handler, agent_factory, session_store, consolidation_queue


def _make_context() -> MessageContext:
    return MessageContext(
        text="test",
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
    )


def _make_channel() -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.send_status = AsyncMock()
    ch.thread_id = None
    return ch


# ---------------------------------------------------------------------------
# strtobool
# ---------------------------------------------------------------------------

class TestStrtobool:

    @pytest.mark.parametrize("val", ["y", "yes", "t", "true", "on", "1",
                                     "Y", "YES", "True", "ON"])
    def test_truthy_values_return_true(self, val):
        assert strtobool(val) is True

    @pytest.mark.parametrize("val", ["n", "no", "f", "false", "off", "0",
                                     "N", "NO", "False", "OFF"])
    def test_falsy_values_return_false(self, val):
        assert strtobool(val) is False

    @pytest.mark.parametrize("val", ["maybe", "2", "", "tru", "ye"])
    def test_invalid_values_raise_value_error(self, val):
        with pytest.raises(ValueError):
            strtobool(val)


# ---------------------------------------------------------------------------
# _get_consolidation_config
# ---------------------------------------------------------------------------

class TestGetConsolidationConfig:

    async def test_user_with_config_returns_user_specific_values(self):
        handler, agent_factory, _, _ = _make_handler()
        user_config = MagicMock()
        user_config.consolidation_threshold = 40
        user_config.consolidation_batch_size = 20
        profile = MagicMock()
        profile.config = user_config
        agent_factory.user_repo.get_user = AsyncMock(return_value=profile)

        result = await handler._get_consolidation_config(_USER_ID)

        assert result.threshold == 40
        assert result.batch_size == 20

    async def test_user_not_found_returns_global_config(self):
        handler, agent_factory, _, _ = _make_handler()
        agent_factory.user_repo.get_user = AsyncMock(return_value=None)

        result = await handler._get_consolidation_config(_USER_ID)

        assert result is handler.global_config

    async def test_exception_in_user_repo_returns_global_config(self):
        handler, agent_factory, _, _ = _make_handler()
        agent_factory.user_repo.get_user = AsyncMock(
            side_effect=RuntimeError("Firestore error")
        )

        result = await handler._get_consolidation_config(_USER_ID)

        assert result is handler.global_config


# ---------------------------------------------------------------------------
# validate_model_output
# ---------------------------------------------------------------------------

class TestValidateModelOutput:

    async def test_no_security_port_passes_through_unchanged(self):
        handler, _, _, _ = _make_handler()
        assert handler.security_port is None

        result = await handler.validate_model_output("hello world", _USER_ID)

        assert result == "hello world"

    async def test_critical_risk_returns_blocked_message(self):
        handler, _, _, _ = _make_handler()
        handler.security_port = AsyncMock()
        validation_result = MagicMock()
        validation_result.risk_level = RiskLevel.CRITICAL
        validation_result.patterns_detected = ["injection"]
        handler.security_port.validate = AsyncMock(return_value=validation_result)

        result = await handler.validate_model_output("bad content", _USER_ID)

        assert "blocked" in result.lower() or "unsafe" in result.lower()

    async def test_high_risk_returns_sanitized_text(self):
        handler, _, _, _ = _make_handler()
        handler.security_port = AsyncMock()
        validation_result = MagicMock()
        validation_result.risk_level = RiskLevel.HIGH
        validation_result.sanitized_text = "sanitized version"
        validation_result.patterns_detected = ["suspicious"]
        handler.security_port.validate = AsyncMock(return_value=validation_result)

        result = await handler.validate_model_output("original", _USER_ID)

        assert result == "sanitized version"

    async def test_safe_risk_returns_sanitized_text(self):
        handler, _, _, _ = _make_handler()
        handler.security_port = AsyncMock()
        validation_result = MagicMock()
        validation_result.risk_level = RiskLevel.SAFE
        validation_result.sanitized_text = "safe original"
        handler.security_port.validate = AsyncMock(return_value=validation_result)

        result = await handler.validate_model_output("safe original", _USER_ID)

        assert result == "safe original"

    async def test_exception_in_security_port_returns_original(self):
        handler, _, _, _ = _make_handler()
        handler.security_port = AsyncMock()
        handler.security_port.validate = AsyncMock(side_effect=RuntimeError("port down"))

        result = await handler.validate_model_output("original text", _USER_ID)

        assert result == "original text"


# ---------------------------------------------------------------------------
# _save_history_with_retry
# ---------------------------------------------------------------------------

class TestSaveHistoryWithRetry:

    async def test_success_on_first_attempt(self):
        handler, agent_factory, session_store, _ = _make_handler()
        session_store.append_messages_batch = AsyncMock()

        await handler._save_history_with_retry(
            session_store=session_store,
            session_id=_SESSION_ID,
            user_parts=[],
            history_text="summary",
            response_text="full response",
            owner_id=_USER_ID,
        )

        session_store.append_messages_batch.assert_called_once()

    async def test_transient_error_retries_and_succeeds(self):
        handler, _, session_store, _ = _make_handler()
        call_count = 0

        async def flaky(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("RST_STREAM error")

        session_store.append_messages_batch = AsyncMock(side_effect=flaky)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handler._save_history_with_retry(
                session_store=session_store,
                session_id=_SESSION_ID,
                user_parts=[],
                history_text="summary",
                response_text="full",
                owner_id=_USER_ID,
            )

        assert call_count == 2

    async def test_non_transient_error_raises_immediately(self):
        handler, _, session_store, _ = _make_handler()
        session_store.append_messages_batch = AsyncMock(
            side_effect=ValueError("schema mismatch")
        )

        with pytest.raises(ValueError):
            await handler._save_history_with_retry(
                session_store=session_store,
                session_id=_SESSION_ID,
                user_parts=[],
                history_text="summary",
                response_text="full",
                owner_id=_USER_ID,
                max_attempts=3,
            )

        # Only one attempt — non-transient error raises without retry
        assert session_store.append_messages_batch.call_count == 1

    async def test_all_attempts_exhausted_raises_last_exception(self):
        handler, _, session_store, _ = _make_handler()
        session_store.append_messages_batch = AsyncMock(
            side_effect=RuntimeError("RST_STREAM persistent")
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="RST_STREAM persistent"):
                await handler._save_history_with_retry(
                    session_store=session_store,
                    session_id=_SESSION_ID,
                    user_parts=[],
                    history_text="summary",
                    response_text="full",
                    owner_id=_USER_ID,
                    max_attempts=2,
                )

        assert session_store.append_messages_batch.call_count == 2


# ---------------------------------------------------------------------------
# handle_command
# ---------------------------------------------------------------------------

class TestHandleCommand:

    async def test_admin_cache_reset_success_sends_confirmation(self):
        handler, agent_factory, _, _ = _make_handler()
        channel = _make_channel()
        context = _make_context()

        await handler.handle_command("admin_cache_reset", context, channel)

        agent_factory.invalidate_prompt_cache.assert_called_once()
        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "Cache reset" in sent or "reset" in sent.lower()

    async def test_admin_cache_reset_exception_sends_error(self):
        handler, agent_factory, _, _ = _make_handler()
        agent_factory.invalidate_prompt_cache.side_effect = RuntimeError("cache locked")
        channel = _make_channel()
        context = _make_context()

        await handler.handle_command("admin_cache_reset", context, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "failed" in sent.lower() or "❌" in sent

    async def test_consolidate_no_queue_sends_not_initialized(self):
        handler, _, _, _ = _make_handler(with_queue=False)
        channel = _make_channel()
        context = _make_context()

        await handler.handle_command("consolidate", context, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "not initialized" in sent.lower() or "❌" in sent

    async def test_consolidate_empty_session_sends_no_messages(self):
        handler, _, session_store, _ = _make_handler()
        session_store.load_session = AsyncMock(return_value=None)
        channel = _make_channel()
        context = _make_context()

        await handler.handle_command("consolidate", context, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "No messages" in sent or "no messages" in sent.lower()

    async def test_consolidate_success_enqueues_and_calls_overflow(self):
        overflow = AsyncMock()
        handler, agent_factory, session_store, consolidation_queue = _make_handler(
            overflow_callback=overflow
        )
        channel = _make_channel()
        channel.send_status = AsyncMock()
        context = _make_context()

        # Build a fake session with messages
        from src.domain.llm import Message, MessagePart
        session = MagicMock()
        session.user_id = _USER_ID
        session.session_id = _SESSION_ID
        session.messages = [MagicMock()]  # non-empty
        msg = MagicMock()
        msg.role = "user"
        part = MagicMock()
        part.full_text = "hello"
        part.consolidation_text = None
        part.text = "hello"
        msg.parts = [part]
        msg.created_at = "2026-01-01T00:00:00"
        session.extract_oldest_messages = MagicMock(return_value=[msg])
        session_store.load_session = AsyncMock(return_value=session)

        await handler.handle_command("consolidate", context, channel)

        consolidation_queue.enqueue_batch.assert_called_once()
        overflow.assert_called_once()

    async def test_unknown_command_sends_unknown_message(self):
        handler, _, _, _ = _make_handler()
        channel = _make_channel()
        context = _make_context()

        await handler.handle_command("no_such_command", context, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "no_such_command" in sent
