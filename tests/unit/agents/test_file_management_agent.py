"""
Unit tests for FileManagementAgent.

Zero-LLM agent: no LLM mocks needed.
Mocking: FileConversionService + FileStoragePort.
"""

import os
import re

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent, AgentStatus
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, to_plain
from src.infrastructure.agent_manifest import Intent
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from tests.support.opened_file import opened_body, opened_native


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


# A consumer that reads PNG and PDF natively (EXTERNAL_FILES_RFC §4.3): without a declaration an open is text only.
NATIVE_PNG_PDF = to_plain([NativeFormat("image/png", 5 * 1024 * 1024, 1),
                           NativeFormat("application/pdf", 20 * 1024 * 1024, 100)])


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
    # The read goes through FileConversionService.open (EXTERNAL_FILES_RFC §4.2); the stored file's text is marked
    # with the neutral tag and cut at D20's 60 000 by the store's presenter (P15, D20).

    async def test_fetch_text_success(self, agent, mock_conversion):
        mock_conversion.open = AsyncMock(return_value=opened_body("# Report\n\nSome content here."))
        msg = _make_message(Intent.OPEN_FILE, file_ref="report.docx")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "Report" in response.result
        assert re.search(r'<external_content nonce="[0-9a-f]{16}" ref="report.docx" name="report.docx">',
                         response.result)
        mock_conversion.open.assert_awaited_once()
        assert mock_conversion.open.await_args.args == ("report.docx", "user1", "")

    async def test_fetch_text_returns_full_content(self, agent, mock_conversion):
        long_content = "x" * 10_000
        mock_conversion.open = AsyncMock(return_value=opened_body(long_content))
        msg = _make_message(Intent.OPEN_FILE, file_ref="big.txt")

        response = await agent.execute(msg)

        # the whole text is inside the wrapper: 10 000 characters are under the store's 60 000 cut (D20)
        inner = re.search(r'<external_content nonce="([0-9a-f]{16})"[^>]*>\n(.*)\n</external_content nonce="\1">',
                          response.result, re.S)
        assert inner is not None and len(inner.group(2)) == 10_000
        assert "[System:" not in response.result


# ---------------------------------------------------------------------------
# open_file — binary
# ---------------------------------------------------------------------------

class TestFetchBinary:
    # Native or text is the document service's decision (EXTERNAL_FILES_RFC P16): the agent hands its context's
    # declaration to `open` and attaches the native file the service answers.

    async def test_fetch_binary_returns_file_data(self, agent, mock_conversion):
        mock_conversion.open = AsyncMock(return_value=opened_native("/tmp/photo_abc.png", "image/png"))
        msg = _make_message(Intent.OPEN_FILE, file_ref="photo.png")
        msg.context[NATIVE_FORMATS_KEY] = NATIVE_PNG_PDF

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "photo.png" in response.result
        assert response.metadata["file_data"]["mime_type"] == "image/png"
        assert response.metadata["file_data"]["path"].endswith(".png")
        mock_conversion.open.assert_awaited_once()
        assert mock_conversion.open.await_args.args == ("photo.png", "user1", "")
        assert mock_conversion.open.await_args.kwargs["consumer"] == tuple(NativeFormat(**f) for f in NATIVE_PNG_PDF)
        mock_conversion.resolve_bytes.assert_not_called()

    async def test_fetch_pdf_is_binary(self, agent, mock_conversion):
        """PDFs are native binary — should use binary path."""
        mock_conversion.open = AsyncMock(return_value=opened_native("/tmp/doc_abc.pdf", "application/pdf"))
        msg = _make_message(Intent.OPEN_FILE, file_ref="document.pdf")
        msg.context[NATIVE_FORMATS_KEY] = NATIVE_PNG_PDF

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert response.metadata["file_data"]["mime_type"] == "application/pdf"
        assert mock_conversion.open.await_args.kwargs["consumer"] == tuple(NativeFormat(**f) for f in NATIVE_PNG_PDF)


# ---------------------------------------------------------------------------
# open_file — video (resend as native file attachment)
# ---------------------------------------------------------------------------

class TestFetchVideo:

    def _video_message(self, file_ref="video_generation/user1/ts.mp4", **extra_context):
        msg = MagicMock(spec=AgentMessage)
        msg.task_id = "task1"
        msg.intent = AgentIntent.QUERY
        msg.payload = {"intent": Intent.OPEN_FILE, "file_ref": file_ref}
        msg.context = {"user_id": "user1", "account_id": "acct1", **extra_context}
        return msg

    async def test_resends_video_as_file(self, mock_conversion, mock_storage):
        notification = AsyncMock()
        agent = FileManagementAgent(
            config=_make_config(),
            conversion_service=mock_conversion,
            storage=mock_storage,
            notification=notification,
        )
        mock_conversion.resolve_bytes = AsyncMock(return_value=b"x" * 1000)
        msg = self._video_message(origin_channel_id="C123", origin_platform="slack")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        notification.notify_file_bytes.assert_awaited_once()
        kwargs = notification.notify_file_bytes.call_args.kwargs
        assert kwargs["user_id"] == "user1"
        assert kwargs["account_id"] == "acct1"
        assert kwargs["file_bytes"] == b"x" * 1000
        assert kwargs["channel_id_override"] == "C123"
        assert kwargs["platform_override"] == "slack"
        mock_conversion.resolve_bytes.assert_called_once_with(
            "video_generation/user1/ts.mp4", "user1"
        )

    async def test_oversized_video_skips_upload(self, mock_conversion, mock_storage):
        notification = AsyncMock()
        agent = FileManagementAgent(
            config=_make_config(),
            conversion_service=mock_conversion,
            storage=mock_storage,
            notification=notification,
        )
        mock_conversion.resolve_bytes = AsyncMock(
            return_value=b"x" * (FileManagementAgent.MAX_VIDEO_ATTACH_BYTES + 1)
        )
        msg = self._video_message()

        response = await agent.execute(msg)

        assert response.status == AgentStatus.SUCCESS
        assert "too large" in response.result.lower()
        notification.notify_file_bytes.assert_not_awaited()

    async def test_no_notification_configured_fails(self, mock_conversion, mock_storage):
        agent = FileManagementAgent(
            config=_make_config(),
            conversion_service=mock_conversion,
            storage=mock_storage,
            notification=None,
        )
        mock_conversion.resolve_bytes = AsyncMock(return_value=b"small")
        msg = self._video_message()

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "not configured" in response.error.lower()


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
        mock_conversion.open = AsyncMock(side_effect=FileNotFoundError("not found")
        )
        msg = _make_message(Intent.OPEN_FILE, file_ref="gone.docx")

        response = await agent.execute(msg)

        assert response.status == AgentStatus.FAILED
        assert "not found" in response.error.lower() or "expired" in response.error.lower()

    async def test_conversion_error(self, agent, mock_conversion):
        mock_conversion.open = AsyncMock(side_effect=RuntimeError("conversion failed")
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
