"""`ChatServices` holds the ConversationHandler dependencies both platforms share, once, and `build_conversation_handler`
builds the real handler from it for both factories. Every bundle field lands on the handler; the platform's rich-content
delivery and Slack's channel binding and history source are the only per-platform inputs."""
import dataclasses
from unittest.mock import MagicMock

import pytest

from src.composition.chat_services import ChatServices, build_conversation_handler
from src.handlers.conversation_handler import ConversationHandler

# ChatServices field -> the ConversationHandler attribute that holds it
HELD_AS = {
    "coordinator": "coordinator",
    "agent_factory": "agent_factory",
    "consolidation_queue": "consolidation_queue",
    "consolidation_config": "global_config",
    "audio_service": "audio_service",
    "notification_service": "_notification_service",
    "localization": "_localization",
    "file_conversion_service": "_file_conversion_service",
    "fallback_service": "_fallback_service",
    "short_link_service": "_short_link_service",
    "skill_service": "_skill_service",
    "memory_fact_service": "_memory_facts",
    "fact_answers": "_fact_answers",
    "drive_note_service": "_drive_notes",
    "long_turn_service": "_long_turns",
    "doc_delivery_service": "_doc_delivery_service",
}


def _full_chat() -> ChatServices:
    return ChatServices(**{f.name: MagicMock(name=f.name) for f in dataclasses.fields(ChatServices)})


def test_every_field_of_the_bundle_has_a_place_on_the_handler():
    assert {f.name for f in dataclasses.fields(ChatServices)} == set(HELD_AS) | {"consolidation_service"}


def test_every_shared_service_reaches_the_handler():
    chat = _full_chat()
    rich = MagicMock(name="rich_content")

    handler = build_conversation_handler(chat, rich)

    assert isinstance(handler, ConversationHandler)
    for field, attr in HELD_AS.items():
        assert getattr(handler, attr) is getattr(chat, field), field
    assert handler._rich_content_service is rich
    assert handler._overflow_callback is chat.consolidation_service.consolidate_now


def test_without_a_binding_or_history_the_handler_has_neither():
    handler = build_conversation_handler(_full_chat(), MagicMock())

    assert handler._channel_binding is None and handler._channel_history is None


def test_slacks_binding_and_history_reach_only_the_handler_they_are_given_to():
    binding, history = MagicMock(name="binding"), MagicMock(name="history")

    handler = build_conversation_handler(_full_chat(), MagicMock(), channel_binding=binding, channel_history=history)

    assert handler._channel_binding is binding and handler._channel_history is history


def test_without_a_consolidation_service_there_is_no_overflow_callback():
    chat = ChatServices(coordinator=MagicMock(), agent_factory=MagicMock())

    assert build_conversation_handler(chat, MagicMock())._overflow_callback is None


def test_the_bundle_is_frozen():
    chat = ChatServices(coordinator=MagicMock(), agent_factory=MagicMock())

    with pytest.raises(dataclasses.FrozenInstanceError):
        chat.skill_service = MagicMock()  # type: ignore[misc]
