"""
ContextFormatter - Formats conversation history for prompt injection.

Part of Prompt Design System v3 (RFC).
"""

from typing import List, Dict


class ContextFormatter:
    """Formats conversation history for prompt injection.

    Converts structured conversation history into plain text format
    suitable for prompt injection. Does NOT perform security validation
    (that's SecurityPort's job).

    Examples:
        >>> formatter = ContextFormatter()
        >>> history = [
        ...     {"role": "user", "content": "Hello"},
        ...     {"role": "assistant", "content": "Hi there!"}
        ... ]
        >>> formatted = formatter.format(history)
        >>> print(formatted)
        User: Hello
        Assistant: Hi there!
    """

    def format(self, conversation_history: List[Dict]) -> str:
        """Format conversation history into plain text.

        Args:
            conversation_history: List of message dictionaries with "role", "content", and optional "timestamp" keys

        Returns:
            Formatted conversation as plain text

        Examples:
            >>> formatter = ContextFormatter()
            >>> history = [
            ...     {"role": "user", "content": "What's the weather?"},
            ...     {"role": "assistant", "content": "I don't have real-time weather data."},
            ...     {"role": "user", "content": "Okay, thanks"}
            ... ]
            >>> formatted = formatter.format(history)
        """
        if not conversation_history:
            return ""

        formatted_lines = []
        for message in conversation_history:
            role = message.get("role", "unknown")
            content = message.get("content", "")
            timestamp = message.get("timestamp", "")

            # Capitalize role for readability
            role_display = role.capitalize()

            # Include timestamp if present
            if timestamp:
                formatted_lines.append(f"{role_display} ({timestamp}): {content}")
            else:
                formatted_lines.append(f"{role_display}: {content}")

        return "\n".join(formatted_lines)

    def format_with_limit(
        self,
        conversation_history: List[Dict],
        max_messages: int = 10
    ) -> str:
        """Format conversation history with message limit.

        Args:
            conversation_history: List of message dictionaries
            max_messages: Maximum number of recent messages to include

        Returns:
            Formatted conversation with most recent messages

        Examples:
            >>> formatter = ContextFormatter()
            >>> history = [{"role": "user", "content": f"Message {i}"} for i in range(20)]
            >>> formatted = formatter.format_with_limit(history, max_messages=5)
            >>> # Only last 5 messages included
        """
        if not conversation_history:
            return ""

        # Take only the most recent messages
        recent_messages = conversation_history[-max_messages:]

        return self.format(recent_messages)

    def format_with_token_limit(
        self,
        conversation_history: List[Dict],
        max_tokens: int = 1000
    ) -> str:
        """Format conversation history with approximate token limit.

        Uses simple heuristic: 1 token ≈ 4 characters.

        Args:
            conversation_history: List of message dictionaries
            max_tokens: Approximate maximum tokens to include

        Returns:
            Formatted conversation within token limit

        Examples:
            >>> formatter = ContextFormatter()
            >>> history = [{"role": "user", "content": "A" * 1000} for _ in range(10)]
            >>> formatted = formatter.format_with_token_limit(history, max_tokens=500)
        """
        if not conversation_history:
            return ""

        max_chars = max_tokens * 4  # Heuristic: 1 token ≈ 4 chars
        formatted_lines = []
        current_chars = 0

        # Process messages in reverse order (most recent first)
        for message in reversed(conversation_history):
            role = message.get("role", "unknown")
            content = message.get("content", "")
            role_display = role.capitalize()
            line = f"{role_display}: {content}"

            if current_chars + len(line) > max_chars:
                break

            formatted_lines.insert(0, line)  # Insert at beginning
            current_chars += len(line) + 1  # +1 for newline

        return "\n".join(formatted_lines)
