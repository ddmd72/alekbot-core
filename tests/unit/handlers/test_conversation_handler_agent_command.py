"""
Unit tests for ConversationHandler._handle_agent_command's bind path
(`$agent <type>`).

Coverage:
  - binding to `tutor` (a companion-type descriptor) attaches the descriptor's
    companion_default_config to the resulting ChannelBinding (Critical #1 fix,
    final whole-branch review 2026-08-31 — before this fix, no code path ever
    produced a ChannelBinding with a non-None companion_config).
  - binding to a non-companion agent type (domain_researcher) still produces
    companion_config=None, unchanged from pre-fix behavior.

Uses the REAL AgentDescriptor instances from agent_manifest.py (TUTOR,
DOMAIN_RESEARCHER) registered into a real AgentRegistry, so this exercises the
actual shipped wiring rather than a hand-rolled stand-in descriptor.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.companion_config import CompanionConfig
from src.domain.messaging import MessageContext
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.agent_manifest import DOMAIN_RESEARCHER, TUTOR
from src.infrastructure.agent_registry import AgentRegistry

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_CHANNEL_ID = "C123"


def _make_registry() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(TUTOR)
    registry.register(DOMAIN_RESEARCHER)
    return registry


def _make_handler():
    channel_binding = MagicMock()
    channel_binding.bind = AsyncMock()

    coordinator = MagicMock()
    coordinator._registry = _make_registry()

    handler = ConversationHandler(
        coordinator=coordinator,
        agent_factory=MagicMock(),
        file_service=MagicMock(),
        channel_binding_service=channel_binding,
    )
    return handler, channel_binding


def _make_context() -> MessageContext:
    return MessageContext(
        text="$agent tutor",
        session_id="sess-test",
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        metadata={"channel": _CHANNEL_ID},
    )


def _make_channel() -> MagicMock:
    ch = MagicMock()
    ch.send_message = AsyncMock()
    ch.thread_id = None
    return ch


class TestHandleAgentCommandBind:

    async def test_binding_to_companion_type_attaches_default_companion_config(self):
        handler, channel_binding = _make_handler()
        context = _make_context()
        channel = _make_channel()

        await handler._handle_agent_command("agent tutor", context, channel)

        channel_binding.bind.assert_called_once()
        bound = channel_binding.bind.call_args[0][0]
        assert bound.channel_id == _CHANNEL_ID
        assert bound.agent_type == "tutor"
        assert bound.companion_config == CompanionConfig(window_threshold=20, batch_size=10)

    async def test_binding_to_non_companion_type_leaves_companion_config_none(self):
        handler, channel_binding = _make_handler()
        context = _make_context()
        channel = _make_channel()

        await handler._handle_agent_command("agent domain_researcher", context, channel)

        channel_binding.bind.assert_called_once()
        bound = channel_binding.bind.call_args[0][0]
        assert bound.channel_id == _CHANNEL_ID
        assert bound.agent_type == "domain_researcher"
        assert bound.companion_config is None
