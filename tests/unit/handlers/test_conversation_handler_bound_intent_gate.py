"""CONFIG_OVERRIDE_HIERARCHY_RFC §3 on bound channels: `$agent <type>` is refused when the agent's intent is switched
off for the user (nothing is saved), and a bound turn the gate refused tells the user so and keeps the binding.

The coordinator is a real `AgentCoordinator` with an injected effective-config function for the bind path; the bound
turn drives a coordinator whose `handle_delegation` answers `AgentResponse.intent_unavailable` (the gate itself is
tested where it lives)."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentErrorCode, AgentResponse
from src.domain.channel_binding import ChannelBinding
from src.domain.config_resolution import SystemConfig, resolve
from src.domain.messaging import MessageContext
from src.domain.ui_messages import StatusType, UIMessage
from src.domain.user import UserBotConfig
from src.handlers.conversation_handler import ConversationHandler
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import TUTOR
from src.infrastructure.agent_registry import AgentRegistry
from src.locales import en
from src.services.localization_service import LocalizationService

_CHANNEL_ID = "C123"


def _ui(message: UIMessage) -> str:
    return en.UI_STRINGS[message.value]


def _channel() -> MagicMock:
    ch = MagicMock()
    ch.platform = "slack"
    ch.send_message = AsyncMock()
    ch.send_status_with_phrase = AsyncMock(return_value=("msg-1", "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_flat_response = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.download_file = AsyncMock(return_value=None)
    ch.thread_id = None
    return ch


def _sent(channel: MagicMock):
    return [c.args[0] for c in channel.send_message.await_args_list]


class _Bindings:
    def __init__(self, existing=None) -> None:
        self.saved = existing
        self.bind = AsyncMock(side_effect=self._bind)

    async def _bind(self, binding) -> None:
        self.saved = binding

    async def get(self, channel_id):
        return self.saved


def _bind_handler(intent_access: dict, existing=None):
    registry = AgentRegistry()
    registry.register(TUTOR)
    effective = resolve(SystemConfig(intent_access={"tutor_chat": True}), None, UserBotConfig(intent_access=intent_access))

    async def effective_config(user_id):
        return effective

    coordinator = AgentCoordinator(registry=registry, effective_config=effective_config)
    bindings = _Bindings(existing)
    handler = ConversationHandler(coordinator=coordinator, agent_factory=MagicMock(), channel_binding_service=bindings,
                                  localization=LocalizationService(FileLocalizationAdapter()))
    return handler, bindings


def _command_context() -> MessageContext:
    return MessageContext(text="$agent tutor", session_id="sess-1", user_id="usr-1", account_id="acc-1",
                          language="en", metadata={"channel": _CHANNEL_ID})


class TestBindingRefusal:

    async def test_a_denied_intent_is_refused_and_nothing_is_saved(self):
        tutor_intent = next(iter(TUTOR.capabilities))
        handler, bindings = _bind_handler({tutor_intent: False})
        channel = _channel()

        await handler._handle_agent_command("agent tutor", _command_context(), channel)

        assert _sent(channel) == [_ui(UIMessage.AGENT_BIND_REFUSED)]
        bindings.bind.assert_not_called()
        assert bindings.saved is None

    async def test_a_refusal_leaves_an_existing_binding_as_it_was(self):
        tutor_intent = next(iter(TUTOR.capabilities))
        existing = ChannelBinding(channel_id=_CHANNEL_ID, agent_type="other", intent="other_intent", created_by="usr-1")
        handler, bindings = _bind_handler({tutor_intent: False}, existing=existing)

        await handler._handle_agent_command("agent tutor", _command_context(), _channel())

        assert bindings.saved is existing

    async def test_an_allowed_intent_is_bound_as_before(self):
        handler, bindings = _bind_handler({})
        channel = _channel()

        await handler._handle_agent_command("agent tutor", _command_context(), channel)

        assert bindings.saved is not None and bindings.saved.agent_type == "tutor"
        assert _ui(UIMessage.AGENT_BIND_REFUSED) not in _sent(channel)


def _bound_turn_handler(response: AgentResponse):
    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    coordinator = MagicMock()
    coordinator.handle_delegation = AsyncMock(return_value=response)
    binding = ChannelBinding(channel_id=_CHANNEL_ID, agent_type="domain_researcher", intent="domain_research",
                             created_by="usr-1")
    bindings = _Bindings(binding)
    history = MagicMock()
    history.fetch = AsyncMock(return_value=[])
    handler = ConversationHandler(coordinator=coordinator, agent_factory=agent_factory,
                                  channel_binding_service=bindings, channel_history_source=history,
                                  localization=LocalizationService(FileLocalizationAdapter()))
    return handler, bindings, binding


def _turn_context() -> MessageContext:
    return MessageContext(text="Explain tides", session_id="usr-1:C123", user_id="usr-1", account_id="acc-1",
                          language="en", metadata={"channel": _CHANNEL_ID})


class TestRefusedBoundTurn:

    async def test_the_user_is_told_and_the_binding_is_kept(self):
        response = AgentResponse.intent_unavailable("domain_research", "task-1", "domain_researcher")
        handler, bindings, binding = _bound_turn_handler(response)
        channel = _channel()

        await handler.handle_message(_turn_context(), channel)

        assert _sent(channel) == [_ui(UIMessage.AGENT_BOUND_TURN_REFUSED)]
        assert bindings.saved is binding

    async def test_another_failure_only_shows_the_error_status(self):
        response = AgentResponse.failure("task-1", "domain_researcher", "boom")
        handler, _, _ = _bound_turn_handler(response)
        channel = _channel()

        await handler.handle_message(_turn_context(), channel)

        assert _ui(UIMessage.AGENT_BOUND_TURN_REFUSED) not in _sent(channel)
        assert any(c.args[0] == StatusType.ERROR for c in channel.send_status.await_args_list)


@pytest.mark.parametrize("language", ["uk", "en", "fr", "es"])
def test_every_locale_has_both_refusal_texts(language):
    import importlib
    strings = importlib.import_module(f"src.locales.{language}").UI_STRINGS
    assert strings[UIMessage.AGENT_BIND_REFUSED.value].strip()
    assert strings[UIMessage.AGENT_BOUND_TURN_REFUSED.value].strip()
