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
