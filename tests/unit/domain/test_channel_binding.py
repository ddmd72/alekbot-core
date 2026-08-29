import dataclasses

import pytest

from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig, CompanionTextMode


def test_companion_config_defaults_to_none():
    """Absence = today's stateless default policy (RFC §5)."""
    binding = ChannelBinding(
        channel_id="C123",
        agent_type="doc_generator",
        intent="create_document",
        created_by="user1",
    )
    assert binding.companion_config is None


def test_companion_config_can_be_set():
    config = CompanionConfig(window_threshold=100, batch_size=50, text_mode=CompanionTextMode.FULL)
    binding = ChannelBinding(
        channel_id="C123",
        agent_type="language_tutor",
        intent="tutor_session",
        created_by="user1",
        companion_config=config,
    )
    assert binding.companion_config is config


def test_frozen():
    binding = ChannelBinding(
        channel_id="C123", agent_type="doc_generator", intent="create_document", created_by="user1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.channel_id = "C456"
