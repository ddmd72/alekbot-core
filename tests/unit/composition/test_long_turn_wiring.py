"""Composition wiring: long-turn dependencies reach the adapter factories and
UserAgentFactory (LONG_RUNNING_TURNS_RFC — Task 12).
"""
import dataclasses
import inspect

from src.composition.chat_services import ChatServices
from src.composition.slack_adapter_factory import SlackAdapterFactory
from src.composition.telegram_adapter_factory import TelegramAdapterFactory
from src.composition.user_agent_factory import UserAgentFactory


def test_factories_accept_the_long_turn_dependencies():
    # Both factories take the shared ChatServices bundle, which carries the long-turn service.
    assert "long_turn_service" in {f.name for f in dataclasses.fields(ChatServices)}
    assert inspect.signature(SlackAdapterFactory.create_adapter).parameters["chat"].annotation is ChatServices
    assert inspect.signature(TelegramAdapterFactory.create_adapter).parameters["chat"].annotation is ChatServices
    assert "long_turn_registry" in inspect.signature(UserAgentFactory.__init__).parameters
