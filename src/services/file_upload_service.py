"""
File Upload Service
===================

Adapter-like service that delegates file uploads to the configured LLMPort.
"""

from ..ports.file_service import FileService
from ..ports.llm_port import LLMPort, MessagePart


class FileUploadService(FileService):
    def __init__(self, llm_port: LLMPort):
        self.llm_port = llm_port

    async def upload_file(self, path: str, mime_type: str) -> MessagePart:
        return await self.llm_port.upload_file(path, mime_type)
