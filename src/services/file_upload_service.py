"""
File Upload Service
===================

Adapter-like service that delegates file uploads to the configured LLMService.
"""

from ..ports.file_service import FileService
from ..ports.llm_service import LLMService, MessagePart


class FileUploadService(FileService):
    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        return await self.llm_service.upload_file(path, mime_type)
