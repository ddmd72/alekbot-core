"""
Unit tests for ConversationHandler rich content delivery.

Covers: table, PDF/file, HTML card, link_list-only, and combined (rich + links)
responses from SmartAgent — verifying that structured_data and link_list reach
the correct channel methods.

Routes:
  content_type in _MEDIA_CONTENT_TYPES ("file", "widget", ...)
      → RichContentService.process() → send_message(url)
  other content types ("table", "pdf", ...)
      → response_channel.send_rich_content()
  link_list
      → passed to update_message() or send_chunked_message() as kwarg
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentResponse, AgentStatus
from src.domain.messaging import MessageContext, RichContent, SmartResponse
from src.domain.ui_messages import StatusType
from src.handlers.conversation_handler import ConversationHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"
_STATUS_MSG_ID = "msg-status-123"

_LINK_LIST = [{"anchor": "1", "title": "Full Report", "url": "https://example.com/report.html"}]


def _make_rich(content_type: str, data: dict = None, fallback: str = "") -> RichContent:
    return RichContent(
        content_type=content_type,
        data=data or {"title": "Test", "headers": ["A"], "rows": [{"cells": ["v"]}]},
        fallback_text=fallback,
    )


def _make_success(result) -> AgentResponse:
    return AgentResponse.success(
        task_id="task-1",
        agent_id=f"smart_response_agent_{_USER_ID}",
        result=result,
    )


def _make_context() -> MessageContext:
    return MessageContext(
        text="test question",
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
    )


def _make_channel() -> MagicMock:
    ch = MagicMock()
    ch.channel_id = "C0123"
    ch.send_status_with_phrase = AsyncMock(return_value=(_STATUS_MSG_ID, "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="thinking")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _make_handler(coordinator, rich_content_service=None) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)

    return ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        rich_content_service=rich_content_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRichContentDelivery:
    """Verify that rich content reaches the correct channel method."""

    async def test_table_goes_to_send_rich_content(self):
        """content_type='table' → send_rich_content (not via RichContentService)."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        rich = _make_rich("table")
        coordinator.route_message.return_value = _make_success(
            SmartResponse(text="Here is the table.", structured_data=rich)
        )

        handler = _make_handler(coordinator)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.send_rich_content.assert_awaited_once_with(rich, thread_id=None)
        channel.send_message.assert_not_awaited()

    async def test_pdf_file_uploaded_to_slack(self):
        """content_type='file', filename=*.pdf → RichContentService.process() → Slack upload (no URL returned)."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        rich = _make_rich(
            "file",
            data={"content": "%PDF-1.4 binary content", "filename": "report.pdf", "title": "Report"},
        )
        coordinator.route_message.return_value = _make_success(
            SmartResponse(text="Your PDF report.", structured_data=rich)
        )

        rcs = MagicMock()
        # PDF → platform upload → process() returns None (no GCS URL)
        rcs.process = AsyncMock(return_value=None)

        handler = _make_handler(coordinator, rich_content_service=rcs)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        rcs.process.assert_awaited_once_with(rich, channel.channel_id)
        # No GCS URL → no extra send_message call (text was delivered via update_message)
        channel.send_rich_content.assert_not_awaited()

    async def test_html_file_stored_in_gcs_and_link_sent(self):
        """content_type='file', filename=*.html → GCS upload → URL sent as message."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        rich = _make_rich(
            "file",
            data={"content": "<h1>Report</h1>", "filename": "report.html", "title": "Report"},
        )
        coordinator.route_message.return_value = _make_success(
            SmartResponse(text="Your HTML report.", structured_data=rich)
        )

        _GCS_URL = "https://storage.googleapis.com/bucket/report.html"
        rcs = MagicMock()
        # HTML → GCS upload → process() returns public URL
        rcs.process = AsyncMock(return_value=_GCS_URL)

        handler = _make_handler(coordinator, rich_content_service=rcs)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        rcs.process.assert_awaited_once_with(rich, channel.channel_id)
        # GCS URL sent as plain message so Slack unfurls it
        channel.send_message.assert_awaited_with(_GCS_URL, thread_id=None)
        channel.send_rich_content.assert_not_awaited()

    async def test_link_list_only_no_rich_content(self):
        """SmartResponse with link_list but no rich_content → link_list forwarded to send_chunked_message."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        coordinator.route_message.return_value = _make_success(
            SmartResponse(text="See [Full Report][1].", link_list=_LINK_LIST)
        )

        handler = _make_handler(coordinator)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.send_rich_content.assert_not_awaited()
        # No structured_data → send_chunked_message with link_list
        channel.send_chunked_message.assert_awaited_once()
        call_kwargs = channel.send_chunked_message.call_args[1]
        assert call_kwargs.get("link_list") == _LINK_LIST

    async def test_rich_content_and_link_list_both_delivered(self):
        """SmartResponse with table + link_list → update_message(link_list) + send_rich_content."""
        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()
        rich = _make_rich("table")
        coordinator.route_message.return_value = _make_success(
            SmartResponse(
                text="Research done. [Full Report][1].",
                structured_data=rich,
                link_list=_LINK_LIST,
            )
        )

        handler = _make_handler(coordinator)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        # Text with link_list goes to send_chunked_message (replaces status bubble, supports long text)
        channel.send_chunked_message.assert_awaited_once()
        chunked_kwargs = channel.send_chunked_message.call_args[1]
        assert chunked_kwargs.get("link_list") == _LINK_LIST

        # Table goes to send_rich_content
        channel.send_rich_content.assert_awaited_once_with(rich, thread_id=None)
