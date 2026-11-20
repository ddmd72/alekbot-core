"""
Unit tests for ContextFormatter.

Tests conversation history formatting.
"""

import pytest

from src.services.prompt_v3.context_formatter import ContextFormatter


def test_context_formatter_empty_history():
    """Test ContextFormatter with empty history."""
    formatter = ContextFormatter()
    result = formatter.format([])
    assert result == ""


def test_context_formatter_single_message():
    """Test ContextFormatter with single message."""
    formatter = ContextFormatter()
    history = [{"role": "user", "content": "Hello"}]

    result = formatter.format(history)

    assert result == "User: Hello"


def test_context_formatter_multiple_messages():
    """Test ContextFormatter with multiple messages."""
    formatter = ContextFormatter()
    history = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "How are you?"}
    ]

    result = formatter.format(history)

    expected = "User: Hello\nAssistant: Hi there!\nUser: How are you?"
    assert result == expected


def test_context_formatter_handles_missing_keys():
    """Test ContextFormatter handles missing role/content keys."""
    formatter = ContextFormatter()
    history = [
        {"role": "user"},  # Missing content
        {"content": "Hello"}  # Missing role
    ]

    result = formatter.format(history)

    assert "User:" in result
    assert "Unknown: Hello" in result
