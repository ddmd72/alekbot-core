"""
Platform-agnostic message chunking utility.
"""
from typing import List


class MessageChunker:
    """
    Split long messages into chunks with smart boundaries.

    Usage:
        chunker = MessageChunker(max_length=4000)
        chunks = chunker.split(long_text)
    """

    def __init__(self, max_length: int, separator: str = "\n\n"):
        self.max_length = max_length
        self.separator = separator

    def split(self, text: str) -> List[str]:
        """
        Split text into chunks at natural boundaries.

        Tries boundaries in order: paragraph → line → sentence → word → character

        Args:
            text: Text to split

        Returns:
            List of chunks (each <= max_length)
        """
        if len(text) <= self.max_length:
            return [text]

        chunks: List[str] = []
        remaining = text

        while len(remaining) > self.max_length:
            # Try paragraph boundary
            split_index = remaining.rfind("\n\n", 0, self.max_length)

            if split_index == -1:
                # Try line boundary
                split_index = remaining.rfind("\n", 0, self.max_length)

            if split_index == -1:
                # Try sentence boundary
                split_index = remaining.rfind(". ", 0, self.max_length)

            if split_index == -1:
                # Try word boundary
                split_index = remaining.rfind(" ", 0, self.max_length)

            if split_index <= 0:
                # Force split at max_length. Covers both no-boundary (-1) and a
                # boundary at index 0 (e.g. ". " starting the remainder): the latter
                # would yield an empty chunk and leave `remaining` unchanged → infinite loop.
                split_index = self.max_length

            chunk = remaining[:split_index].rstrip()
            if chunk:
                chunks.append(chunk)

            remaining = remaining[split_index:].lstrip()

        if remaining:
            chunks.append(remaining)

        return chunks
