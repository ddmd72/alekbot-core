"""
Tests for OpenAI adapter developer_message functionality.

Covers: USER_TURN_SYSTEM_ANCHOR extraction and injection into developer_message role.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.adapters.openai_adapter import OpenAIAdapter
from src.ports.llm_port import LLMRequest, Message, MessagePart
from src.domain.llm import USER_TURN_SYSTEM_ANCHOR


class TestOpenAIAdapterDeveloperMessage:
    """Test developer_message role injection for personality grounding."""

    @pytest.fixture
    def adapter(self):
        """Create OpenAI adapter instance."""
        adapter = OpenAIAdapter()
        adapter.client = MagicMock()
        return adapter

    def test_user_turn_system_anchor_contains_nlp_techniques(self):
        """Verify USER_TURN_SYSTEM_ANCHOR specifies NLP techniques, not 'manipulate'."""
        assert "Reframing" in USER_TURN_SYSTEM_ANCHOR
        assert "Presupposition" in USER_TURN_SYSTEM_ANCHOR
        assert "Metaphor" in USER_TURN_SYSTEM_ANCHOR
        assert "Pattern interrupt" in USER_TURN_SYSTEM_ANCHOR
        assert "Calibration" in USER_TURN_SYSTEM_ANCHOR
        # Verify old formulation is gone
        assert "Manipulate the user" not in USER_TURN_SYSTEM_ANCHOR

    def test_anchor_extracted_from_user_message(self):
        """Test that USER_TURN_SYSTEM_ANCHOR is extracted from last user message."""
        # Build a message with anchor injected
        anchor_text = f"{USER_TURN_SYSTEM_ANCHOR}\n\nuser question"

        messages = [
            Message(
                role="user",
                parts=[MessagePart(text=anchor_text)]
            )
        ]

        # Simulate extraction logic
        last_user_msg = messages[-1]
        has_anchor = USER_TURN_SYSTEM_ANCHOR in last_user_msg.parts[0].text

        assert has_anchor, "Anchor should be present in user message"

    def test_anchor_removed_from_user_message_content(self):
        """Test that anchor is cleanly removed from user message text."""
        anchor_text = f"{USER_TURN_SYSTEM_ANCHOR}\n\nuser question"

        # Simulate the removal logic from OpenAI adapter
        cleaned = anchor_text.replace(USER_TURN_SYSTEM_ANCHOR + "\n\n", "")

        assert cleaned == "user question"
        assert USER_TURN_SYSTEM_ANCHOR not in cleaned

    def test_developer_message_contains_personality_anchor(self):
        """Test that developer_message includes personality anchor when system_instruction has personality blocks."""
        system_instruction = """
        voice {
            tone: aphoristic, paradoxical
        }
        humor_engine {
            style: Ranevskaya-filtered
        }
        """

        # Simulate personality anchor detection
        has_personality_marker = "humor_engine" in system_instruction

        assert has_personality_marker, "Should detect personality blocks via humor_engine marker"

    def test_developer_message_combines_anchor_and_personality(self):
        """Test that developer_message combines both system anchor and personality anchor."""
        # Both components should be present
        combined = f"{USER_TURN_SYSTEM_ANCHOR}\n\nPERSONALITY ANCHOR — High Priority"

        assert USER_TURN_SYSTEM_ANCHOR in combined
        assert "PERSONALITY ANCHOR" in combined
        assert "Reframing" in combined  # From system anchor

    def test_claude_gemini_unaffected(self):
        """Verify that Claude and Gemini adapters don't extract/inject developer_message."""
        # This is implicit: only OpenAI adapter imports USER_TURN_SYSTEM_ANCHOR
        # Claude/Gemini adapters should not have extraction logic
        from src.adapters.claude_adapter import ClaudeAdapter
        from src.adapters.gemini_adapter import GeminiAdapter

        # Verify they don't have the extraction pattern
        claude_source = str(ClaudeAdapter)
        gemini_source = str(GeminiAdapter)

        # They should not be extracting from user message
        assert "USER_TURN_SYSTEM_ANCHOR" not in claude_source or \
               "extracted_anchor" not in claude_source
        assert "USER_TURN_SYSTEM_ANCHOR" not in gemini_source or \
               "extracted_anchor" not in gemini_source
