import pytest
from unittest.mock import AsyncMock

from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.llm import Message, MessagePart


@pytest.mark.asyncio
async def test_call_summary_message_pair_serializes_for_consolidation():
    """RFC §7 item 11: verify a spoken conversation's summary consolidates
    into facts by the ordinary path - no voice-specific protocol. This is
    the exact two-message shape notify_call_summary (Task 15) writes."""
    messages = [
        Message(role="user", parts=[MessagePart(text="[System: phone call ended]")]),
        Message(role="model", parts=[MessagePart(text="Discussed Q3 budget with Ivan.", full_text="Discussed Q3 budget with Ivan.")]),
    ]

    serialized = serialize_messages_for_consolidation(messages)

    assert serialized[0]["parts"][0]["text"] == "[System: phone call ended]"
    assert serialized[1]["parts"][0]["text"] == "Discussed Q3 budget with Ivan."
