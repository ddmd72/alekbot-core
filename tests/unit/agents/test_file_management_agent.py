"""
Unit tests for FileManagementAgent.

Zero-LLM agent: no LLM mocks needed.
Mocking: FileConversionService + FileStoragePort.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config():
    return AgentConfig(
        agent_id="file_management_agent_user1",
        agent_type="file_management",
        capabilities={},
        metadata={"user_id": "user1"},
    )


def _make_message(intent: str, file_ref: str = None, user_id: str = "user1"):
    payload = {"intent": intent}
    if file_ref:
        payload["file_ref"] = file_ref
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "task1"
    msg.intent = AgentIntent.QUERY
    msg.payload = payload
    msg.context = {"user_id": user_id}
    return msg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_conversion():
    return AsyncMock(spec=FileConversionService)


@pytest.fixture
def mock_storage():
    return AsyncMock(spec=FileStoragePort)


@pytest.fixture
def agent(mock_conversion, mock_storage):
    return FileManagementAgent(
        config=_make_config(),
        conversion_service=mock_conversion,
        storage=mock_storage,
    )


# ---------------------------------------------------------------------------
# can_handle()
# ---------------------------------------------------------------------------

class TestCanHandle:

    async def test_handles_query_intent(self, agent):
        msg = MagicMock(spec=AgentMessage)
        msg.intent = AgentIntent.QUERY
        assert await agent.can_handle(msg) is True

    async def test_rejects_non_query_intent(self, agent):
        msg = MagicMock(spec=AgentMessage)
        msg.intent = AgentIntent.DELEGATE
        assert await agent.can_handle(msg) is False


# ---------------------------------------------------------------------------
# open_file — text
# ---------------------------------------------------------------------------

class TestFetchText:

    async def test_fetch_text_success(self, agent, mock_conversion):
        mock_conversion.resolve_content = AsyncMock(
            return_value="# Report\n\nSome content here."
        )
        msg = _make_message(Intent.OPEN_FILE, file_ref="report.docx")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "Report" in response.result
        mock_conversion.resolve_content.assert_called_once_with("report.docx", "user1")

    async def test_fetch_text_returns_full_content(self, agent, mock_conversion):
        long_content = "x" * 10_000
        mock_conversion.resolve_content = AsyncMock(return_value=long_content)
        msg = _make_message(Intent.OPEN_FILE, file_ref="big.txt")

        response = await agent.execute(msg)

        assert len(response.result) == 10_000


# ---------------------------------------------------------------------------
# open_file — binary
# ---------------------------------------------------------------------------

class TestFetchBinary:

    async def test_fetch_binary_returns_file_data(self, agent, mock_conversion):
        # Binary path resolves via the conversion service (symmetric with the text
        # path), so delivered-document keys resolve with the ownership check too.
        mock_conversion.resolve_bytes = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
        msg = _make_message(Intent.OPEN_FILE, file_ref="photo.png")

        with patch("src.agents.file_management_agent.tempfile") as mock_tempfile, \
             patch("src.agents.file_management_agent.aiofiles") as mock_aiofiles, \
             patch("src.agents.file_management_agent.os") as mock_os:
            mock_tempfile.mkstemp.return_value = (5, "/tmp/photo_abc.png")
            mock_aio_ctx = AsyncMock()
            mock_aiofiles.open.return_value.__aenter__ = mock_aio_ctx
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "photo.png" in response.result
        assert response.metadata["file_data"]["mime_type"] == "image/png"
        assert response.metadata["file_data"]["path"].endswith(".png")
        mock_conversion.resolve_bytes.assert_called_once_with("photo.png", "user1")

    async def test_fetch_pdf_is_binary(self, agent, mock_conversion):
        """PDFs are native binary — should use binary path."""
        mock_conversion.resolve_bytes = AsyncMock(return_value=b"%PDF-1.4")
        msg = _make_message(Intent.OPEN_FILE, file_ref="document.pdf")

        with patch("src.agents.file_management_agent.tempfile") as mock_tempfile, \
             patch("src.agents.file_management_agent.aiofiles") as mock_aiofiles, \
             patch("src.agents.file_management_agent.os") as mock_os:
            mock_tempfile.mkstemp.return_value = (5, "/tmp/doc_abc.pdf")
            mock_aio_ctx = AsyncMock()
            mock_aiofiles.open.return_value.__aenter__ = mock_aio_ctx
            mock_aiofiles.open.return_value.__aexit__ = AsyncMock()

            response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["file_data"]["mime_type"] == "application/pdf"


# ---------------------------------------------------------------------------
# open_file — errors
# ---------------------------------------------------------------------------

class TestFetchErrors:

    async def test_missing_file_ref(self, agent):
        msg = _make_message(Intent.OPEN_FILE, file_ref=None)

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "file_ref" in response.error
        assert "required" in response.error

    async def test_file_not_found(self, agent, mock_conversion):
        mock_conversion.resolve_content = AsyncMock(
            side_effect=FileNotFoundError("not found")
        )
        msg = _make_message(Intent.OPEN_FILE, file_ref="gone.docx")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "not found" in response.error.lower() or "expired" in response.error.lower()

    async def test_conversion_error(self, agent, mock_conversion):
        mock_conversion.resolve_content = AsyncMock(
            side_effect=RuntimeError("conversion failed")
        )
        msg = _make_message(Intent.OPEN_FILE, file_ref="broken.docx")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "RuntimeError" in response.error


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------

class TestDelete:

    async def test_delete_success(self, agent, mock_storage):
        mock_storage.delete = AsyncMock()
        msg = _make_message(Intent.DELETE_FILE, file_ref="old.txt")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "deleted" in response.result.lower()
        mock_storage.delete.assert_called_once_with("old.txt", "user1")

    async def test_delete_missing_file_ref(self, agent):
        msg = _make_message(Intent.DELETE_FILE, file_ref=None)

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "file_ref" in response.error

    async def test_delete_file_not_found(self, agent, mock_storage):
        mock_storage.delete = AsyncMock(side_effect=FileNotFoundError("not found"))
        msg = _make_message(Intent.DELETE_FILE, file_ref="ghost.txt")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "not found" in response.error.lower()

    async def test_delete_generic_error(self, agent, mock_storage):
        mock_storage.delete = AsyncMock(side_effect=RuntimeError("boom"))
        msg = _make_message(Intent.DELETE_FILE, file_ref="err.txt")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "RuntimeError" in response.error


# ---------------------------------------------------------------------------
# Unknown intent
# ---------------------------------------------------------------------------

class TestUnknownIntent:

    async def test_unknown_intent_returns_failure(self, agent):
        msg = _make_message("some_unknown_intent", file_ref="file.txt")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "some_unknown_intent" in response.error
        assert "open_file" in response.error  # suggests valid intents
