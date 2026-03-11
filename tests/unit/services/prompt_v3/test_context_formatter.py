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


def test_context_formatter_format_with_limit():
    """Test ContextFormatter with message limit."""
    formatter = ContextFormatter()
    history = [
        {"role": "user", "content": f"Message {i}"}
        for i in range(10)
    ]

    result = formatter.format_with_limit(history, max_messages=3)

    # Should only include last 3 messages
    assert "Message 7" in result
    assert "Message 8" in result
    assert "Message 9" in result
    assert "Message 0" not in result


def test_context_formatter_format_with_token_limit():
    """Test ContextFormatter with token limit."""
    formatter = ContextFormatter()
    history = [
        {"role": "user", "content": "A" * 100},  # ~100 chars
        {"role": "user", "content": "B" * 100},
        {"role": "user", "content": "C" * 100}
    ]

    # max_tokens=50 → max_chars=200 → should fit 1-2 messages
    result = formatter.format_with_token_limit(history, max_tokens=50)

    assert "C" in result  # Most recent message included
    assert len(result) <= 250  # Approximate limit


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
