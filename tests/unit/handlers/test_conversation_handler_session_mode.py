"""
Unit tests for ConversationHandler._resolve_session_mode.

Coverage:
  _resolve_session_mode
    - no binding                          → default SessionMode (full orchestrator flow)
    - binding without companion_config    → unchanged stateless bound-channel mode
    - binding with companion_config       → stateful, companion write_session_id
    - companion write_session_id uses the real platform argument (not hardcoded)
"""
from unittest.mock import MagicMock

from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.handlers.conversation_handler import ConversationHandler


def _make_handler() -> ConversationHandler:
    # _resolve_session_mode reads no instance state — every collaborator can be a
    # bare MagicMock(). Positional/required args mirror the minimal construction
    # pattern in test_conversation_handler_utils.py::_make_handler.
    return ConversationHandler(
        coordinator=MagicMock(),
        agent_factory=MagicMock(),
        file_service=MagicMock(),
    )


class TestResolveSessionMode:

    def test_no_binding_returns_default_mode(self):
        handler = _make_handler()
        mode = handler._resolve_session_mode("C1", None, "slack")
        assert mode.is_bound is False
        assert mode.write_session is True
        assert mode.write_session_id is None

    def test_binding_without_companion_config_is_unchanged_stateless(self):
        handler = _make_handler()
        binding = ChannelBinding(
            channel_id="C1", agent_type="domain_researcher", intent="domain_research",
            created_by="user-1", companion_config=None,
        )
        mode = handler._resolve_session_mode("C1", binding, "slack")
        assert mode.is_bound is True
        assert mode.route_intent == "domain_research"
        assert mode.write_session is False
        assert mode.write_session_id is None
        assert mode.history_source == "platform"

    def test_binding_with_companion_config_is_stateful_with_companion_session_id(self):
        handler = _make_handler()
        binding = ChannelBinding(
            channel_id="C1", agent_type="tutor", intent="tutor_chat",
            created_by="user-1",
            companion_config=CompanionConfig(window_threshold=100, batch_size=50),
        )
        mode = handler._resolve_session_mode("C1", binding, "slack")
        assert mode.is_bound is True
        assert mode.route_intent == "tutor_chat"
        assert mode.write_session is True
        assert mode.write_session_id == "slack:C1"
        assert mode.history_source == "session_store"  # Phase G: companion reads from SessionStore
        assert mode.write_consolidation is False
        assert mode.update_notification_channel is False
        assert mode.use_threads is False

    def test_companion_session_id_uses_the_real_platform_argument(self):
        handler = _make_handler()
        binding = ChannelBinding(
            channel_id="C2", agent_type="tutor", intent="tutor_chat",
            created_by="user-1",
            companion_config=CompanionConfig(window_threshold=100, batch_size=50),
        )
        mode = handler._resolve_session_mode("C2", binding, "telegram")
        assert mode.write_session_id == "telegram:C2"
