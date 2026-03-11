"""
File Service Port
=================

Abstract interface for uploading files to LLM providers.
"""

from abc import ABC, abstractmethod
from ..domain.llm import MessagePart


class FileService(ABC):
    @abstractmethod
    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        """Upload a file and return LLM-compatible MessagePart."""
        raise NotImplementedError
