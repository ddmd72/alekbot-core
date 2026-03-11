import pytest
from unittest.mock import AsyncMock
from src.adapters.slack.response_channel import SlackResponseChannel


@pytest.mark.requirement("REQ-UI-10")
@pytest.mark.asyncio
async def test_status_animation_keeps_phrase_constant():
    """
    Verify status animation updates dots without changing the phrase.
    Covers: REQ-UI-10 (Deterministic Status Animation)

    Self-validation:
    - Calls real update_status_with_phrase_and_dots: ✅
    - Fails if message format changes: ✅
    """
    client = AsyncMock()
    channel = SlackResponseChannel(client, "C1", "token")
    channel.update_message = AsyncMock()

    await channel.update_status_with_phrase_and_dots("msg-1", "Thinking", 3)

    channel.update_message.assert_called_once_with("msg-1", "⏳ Thinking...")
