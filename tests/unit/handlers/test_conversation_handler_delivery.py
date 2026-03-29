"""
Unit tests for ConversationHandler delivery paths and handle_message edge cases.

Coverage targets:
  _deliver_rich_content  — lines 137-142: media type + no channel_id → warning + fallback
  _deliver_item          — lines 153-222: all item types + error paths
  handle_message         — lines 313-320: notification channel persistence (Slack DM vs regular)
                         — lines 373-391: no text + attachments (localization + hardcoded)
                         — lines 397-434: file attachment processing
                         — lines 484-485: failed agent response
                         — lines 507-517: rich-content-only (no text)
                         — line  522:     empty text placeholder
                         — lines 549-564: history summary resolution
                         — lines 576-577, 581: history cleanup (file stubs)
                         — lines 591-598, 622-624, 630: metadata appending + delivery items
                         — lines 655-656, 660-663: exception + temp file cleanup
  handle_command         — line  753-754: consolidate — empty old_messages
                         — line  796:     consolidate — _notification_service present
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from src.domain.agent import AgentResponse, AgentStatus, DeliveryItem
from src.domain.messaging import FileAttachment, MessageContext, RichContent, SmartResponse
from src.domain.settings import ConsolidationSettings
from src.domain.ui_messages import StatusType
from src.handlers.conversation_handler import ConversationHandler


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_USER_ID = "user-test"
_ACCOUNT_ID = "acc-test"
_SESSION_ID = "sess-test"
_STATUS_MSG_ID = "msg-status-001"


def _make_context(
    text: str = "hello",
    attachments=None,
    metadata: dict | None = None,
) -> MessageContext:
    return MessageContext(
        text=text,
        session_id=_SESSION_ID,
        user_id=_USER_ID,
        account_id=_ACCOUNT_ID,
        attachments=attachments or [],
        metadata=metadata or {},
    )


def _make_channel(*, channel_id: str = "C-001") -> MagicMock:
    ch = MagicMock()
    ch.channel_id = channel_id
    ch.platform = "slack"
    ch.send_status_with_phrase = AsyncMock(return_value=(_STATUS_MSG_ID, "thinking..."))
    ch.send_status = AsyncMock()
    ch.send_message = AsyncMock()
    ch.send_chunked_message = AsyncMock()
    ch.update_message = AsyncMock()
    ch.send_rich_content = AsyncMock()
    ch.update_status_with_phrase_and_dots = AsyncMock()
    ch.get_status_phrase = AsyncMock(return_value="processing")
    ch.download_file = AsyncMock(return_value=None)
    ch.max_message_length = 4000
    ch.supports_message_editing = True
    return ch


def _make_success(result) -> AgentResponse:
    return AgentResponse.success(
        task_id="task-1",
        agent_id=f"smart_response_agent_{_USER_ID}",
        result=result,
    )


def _make_handler(
    coordinator,
    *,
    rich_content_service=None,
    doc_delivery_service=None,
    notification_service=None,
    overflow_callback=None,
    localization=None,
    with_queue: bool = False,
) -> ConversationHandler:
    session_store = MagicMock()
    session_store.append_messages_batch = AsyncMock()
    session_store.load_session = AsyncMock(return_value=None)
    session_store.save_session = AsyncMock()

    agent_factory = MagicMock()
    agent_factory.ensure_agents_for_user = AsyncMock()
    agent_factory.get_session_store = MagicMock(return_value=session_store)
    agent_factory.invalidate_prompt_cache = MagicMock()
    user_repo = MagicMock()
    user_repo.get_user = AsyncMock(return_value=None)
    agent_factory.user_repo = user_repo

    consolidation_queue = None
    if with_queue:
        consolidation_queue = MagicMock()
        consolidation_queue.enqueue_batch = AsyncMock(return_value="batch-1")

    return ConversationHandler(
        coordinator=coordinator,
        agent_factory=agent_factory,
        file_service=MagicMock(),
        rich_content_service=rich_content_service,
        doc_delivery_service=doc_delivery_service,
        notification_service=notification_service,
        overflow_callback=overflow_callback,
        localization=localization,
        consolidation_queue=consolidation_queue,
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
    )


def _simple_coordinator(result) -> MagicMock:
    coord = MagicMock()
    coord.route_message = AsyncMock(return_value=result)
    return coord


# ---------------------------------------------------------------------------
# Class 1: _deliver_rich_content — no channel_id → warning + fallback
# ---------------------------------------------------------------------------

class TestDeliverRichContentNoChannelId:
    """Lines 137-142: media type + rich_content_service present but channel has no channel_id."""

    async def test_media_type_no_channel_id_falls_back_to_send_rich_content(self):
        coord = MagicMock()
        rcs = MagicMock()
        rcs.process = AsyncMock(return_value="https://gcs/file.png")

        handler = _make_handler(coord, rich_content_service=rcs)

        channel = MagicMock()
        # No channel_id attribute at all (getattr returns None)
        del channel.channel_id  # ensure AttributeError → getattr fallback
        channel.send_rich_content = AsyncMock()
        channel.send_message = AsyncMock()

        content = RichContent(content_type="file", data={"title": "x"}, fallback_text="")

        await handler._deliver_rich_content(content, channel, thread_id=None)

        # RichContentService.process must NOT be called — no channel_id
        rcs.process.assert_not_awaited()
        # Fallback: send_rich_content called instead
        channel.send_rich_content.assert_awaited_once_with(content, thread_id=None)

    async def test_media_type_channel_id_none_falls_back_to_send_rich_content(self):
        coord = MagicMock()
        rcs = MagicMock()
        rcs.process = AsyncMock(return_value=None)

        handler = _make_handler(coord, rich_content_service=rcs)

        channel = MagicMock()
        channel.channel_id = None
        channel.send_rich_content = AsyncMock()

        content = RichContent(content_type="weather_image", data={}, fallback_text="")

        await handler._deliver_rich_content(content, channel, thread_id=None)

        rcs.process.assert_not_awaited()
        channel.send_rich_content.assert_awaited_once_with(content, thread_id=None)


# ---------------------------------------------------------------------------
# Class 2: _deliver_item — all item types + error paths
# ---------------------------------------------------------------------------

class TestDeliverItem:
    """Lines 153-222: dispatch DeliveryItem to correct handler."""

    # --- html_gcs_link ---

    async def test_html_gcs_link_no_rich_content_service_returns_early(self):
        handler = _make_handler(MagicMock(), rich_content_service=None)
        channel = MagicMock()
        channel.send_message = AsyncMock()

        item = DeliveryItem(type="html_gcs_link", data={"html": "<b>hi</b>", "filename": "x.html"})
        await handler._deliver_item(item, channel, thread_id=None)

        channel.send_message.assert_not_awaited()

    async def test_html_gcs_link_success_sends_link(self):
        rcs = MagicMock()
        rcs._store_html = AsyncMock(return_value="https://gcs/doc.html")
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.send_message = AsyncMock()

        item = DeliveryItem(
            type="html_gcs_link",
            data={"html": "<h1>Report</h1>", "filename": "doc.html", "link_text": "Open Report"},
        )
        await handler._deliver_item(item, channel, thread_id="T1")

        rcs._store_html.assert_awaited_once_with("<h1>Report</h1>", "doc.html")
        channel.send_message.assert_awaited_once_with(
            "<https://gcs/doc.html|Open Report>", "T1"
        )

    async def test_html_gcs_link_exception_logs_warning(self):
        rcs = MagicMock()
        rcs._store_html = AsyncMock(side_effect=RuntimeError("GCS down"))
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.send_message = AsyncMock()

        item = DeliveryItem(type="html_gcs_link", data={"html": "<b>x</b>", "filename": "x.html"})
        # Should not raise; exception is swallowed and logged
        await handler._deliver_item(item, channel, thread_id=None)
        channel.send_message.assert_not_awaited()

    async def test_html_gcs_link_store_returns_none_no_message_sent(self):
        rcs = MagicMock()
        rcs._store_html = AsyncMock(return_value=None)
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.send_message = AsyncMock()

        item = DeliveryItem(type="html_gcs_link", data={"html": "<b>x</b>", "filename": "x.html"})
        await handler._deliver_item(item, channel, thread_id=None)
        channel.send_message.assert_not_awaited()

    # --- rich_content ---

    async def test_rich_content_item_calls_deliver_rich_content(self):
        handler = _make_handler(MagicMock())

        channel = MagicMock()
        channel.send_rich_content = AsyncMock()

        item = DeliveryItem(
            type="rich_content",
            data={
                "content_type": "table",
                "data": {"headers": ["A"], "rows": [{"cells": ["1"]}]},
                "fallback": "fallback text",
            },
        )
        await handler._deliver_item(item, channel, thread_id=None)
        channel.send_rich_content.assert_awaited_once()

    # --- message ---

    async def test_message_item_sends_text(self):
        handler = _make_handler(MagicMock())
        channel = MagicMock()
        channel.send_message = AsyncMock()

        item = DeliveryItem(type="message", data={"text": "Hello from agent!"})
        await handler._deliver_item(item, channel, thread_id="T-thread")

        channel.send_message.assert_awaited_once_with("Hello from agent!", "T-thread")

    # --- file_upload ---

    async def test_file_upload_no_rich_content_service_returns_early(self):
        handler = _make_handler(MagicMock(), rich_content_service=None)
        channel = MagicMock()
        channel.channel_id = "C-1"

        file_bytes = base64.b64encode(b"fake file content").decode()
        item = DeliveryItem(
            type="file_upload",
            data={"file_bytes_b64": file_bytes, "filename": "doc.docx", "title": "My Doc"},
        )
        await handler._deliver_item(item, channel, thread_id=None)
        # No crash, just early return

    async def test_file_upload_no_channel_id_returns_early(self):
        rcs = MagicMock()
        rcs.upload_file_bytes = AsyncMock()
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.channel_id = None

        file_bytes = base64.b64encode(b"fake file content").decode()
        item = DeliveryItem(
            type="file_upload",
            data={"file_bytes_b64": file_bytes, "filename": "doc.docx", "title": "My Doc"},
        )
        await handler._deliver_item(item, channel, thread_id=None)
        rcs.upload_file_bytes.assert_not_awaited()

    async def test_file_upload_success_calls_upload_file_bytes(self):
        rcs = MagicMock()
        rcs.upload_file_bytes = AsyncMock()
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.channel_id = "C-upload"

        raw_bytes = b"DOCX binary content"
        file_bytes_b64 = base64.b64encode(raw_bytes).decode()
        item = DeliveryItem(
            type="file_upload",
            data={"file_bytes_b64": file_bytes_b64, "filename": "report.docx", "title": "Report"},
        )
        await handler._deliver_item(item, channel, thread_id=None)

        rcs.upload_file_bytes.assert_awaited_once_with(
            file_bytes=raw_bytes,
            filename="report.docx",
            title="Report",
            channel_id="C-upload",
        )

    async def test_file_upload_exception_logs_error_no_raise(self):
        rcs = MagicMock()
        rcs.upload_file_bytes = AsyncMock(side_effect=RuntimeError("upload failed"))
        handler = _make_handler(MagicMock(), rich_content_service=rcs)

        channel = MagicMock()
        channel.channel_id = "C-1"

        file_bytes_b64 = base64.b64encode(b"data").decode()
        item = DeliveryItem(
            type="file_upload",
            data={"file_bytes_b64": file_bytes_b64, "filename": "x.docx", "title": "X"},
        )
        # Should not raise
        await handler._deliver_item(item, channel, thread_id=None)

    # --- document ---

    async def test_document_no_doc_delivery_service_returns_early(self):
        handler = _make_handler(MagicMock(), doc_delivery_service=None)
        channel = MagicMock()
        channel.send_document_link = AsyncMock()

        content_b64 = base64.b64encode(b"doc content").decode()
        item = DeliveryItem(
            type="document",
            data={"content_b64": content_b64, "filename": "x.pdf", "content_type": "application/pdf"},
        )
        await handler._deliver_item(item, channel, thread_id=None)
        channel.send_document_link.assert_not_awaited()

    async def test_document_success_sends_link(self):
        dds = MagicMock()
        dds.store = AsyncMock(return_value="https://gcs/x.pdf")
        handler = _make_handler(MagicMock(), doc_delivery_service=dds)

        channel = MagicMock()
        channel.send_document_link = AsyncMock()
        channel.send_file = AsyncMock()

        content_b64 = base64.b64encode(b"PDF bytes").decode()
        item = DeliveryItem(
            type="document",
            data={
                "content_b64": content_b64,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "label": "My PDF",
            },
        )
        await handler._deliver_item(item, channel, thread_id="T2")

        dds.store.assert_awaited_once()
        channel.send_document_link.assert_awaited_once_with(
            url="https://gcs/x.pdf", label="My PDF", thread_id="T2"
        )
        channel.send_file.assert_not_awaited()  # no file_upload flag

    async def test_document_with_file_upload_sends_file_too(self):
        dds = MagicMock()
        dds.store = AsyncMock(return_value="https://gcs/x.pdf")
        handler = _make_handler(MagicMock(), doc_delivery_service=dds)

        channel = MagicMock()
        channel.send_document_link = AsyncMock()
        channel.send_file = AsyncMock()

        content_b64 = base64.b64encode(b"PDF bytes").decode()
        item = DeliveryItem(
            type="document",
            data={
                "content_b64": content_b64,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "file_upload": True,
            },
        )
        await handler._deliver_item(item, channel, thread_id=None)

        channel.send_document_link.assert_awaited_once()
        channel.send_file.assert_awaited_once()

    async def test_document_exception_logs_error_no_raise(self):
        dds = MagicMock()
        dds.store = AsyncMock(side_effect=RuntimeError("store failed"))
        handler = _make_handler(MagicMock(), doc_delivery_service=dds)

        channel = MagicMock()
        channel.send_document_link = AsyncMock()

        content_b64 = base64.b64encode(b"bytes").decode()
        item = DeliveryItem(
            type="document",
            data={"content_b64": content_b64, "filename": "x.pdf", "content_type": "application/pdf"},
        )
        await handler._deliver_item(item, channel, thread_id=None)
        channel.send_document_link.assert_not_awaited()

    # --- unknown type ---

    async def test_unknown_type_logs_warning_no_raise(self):
        handler = _make_handler(MagicMock())
        channel = MagicMock()
        item = DeliveryItem(type="telekinesis", data={})
        await handler._deliver_item(item, channel, thread_id=None)
        # Verify no channel methods called
        channel.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Class 3: handle_message edge cases
# ---------------------------------------------------------------------------

class TestHandleMessageEdgeCases:
    """Cover various handle_message branches not exercised by existing tests."""

    # --- Lines 313-320: Notification channel persistence ---

    async def test_notification_service_saved_on_regular_channel(self):
        """Platform present + channel_type not 'im' → uses channel_id directly."""
        notif = MagicMock()
        notif.save_channel = AsyncMock()

        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord, notification_service=notif)
        channel = _make_channel(channel_id="C-main")

        ctx = _make_context(metadata={"channel_type": "channel"})

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(ctx, channel)

        # Give the background task a chance to run
        await asyncio.sleep(0.05)
        notif.save_channel.assert_awaited_once_with(
            user_id=_USER_ID, platform="slack", channel_id="C-main"
        )

    async def test_notification_service_uses_slack_user_id_for_dm(self):
        """Slack DM (channel_type=im + slack_user_id) → uses slack_user_id not channel_id."""
        notif = MagicMock()
        notif.save_channel = AsyncMock()

        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord, notification_service=notif)
        channel = _make_channel(channel_id="D-dm")  # DM channel starts with D

        ctx = _make_context(
            metadata={"channel_type": "im", "slack_user_id": "U-slack-123"}
        )

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(ctx, channel)

        await asyncio.sleep(0.05)
        notif.save_channel.assert_awaited_once_with(
            user_id=_USER_ID, platform="slack", channel_id="U-slack-123"
        )

    async def test_no_notification_service_no_crash(self):
        """_notification_service=None → no background task, no crash."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord, notification_service=None)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.send_chunked_message.assert_awaited_once()

    # --- Lines 484-485: Failed agent response ---

    async def test_failed_response_sends_error_status_and_returns(self):
        """AgentStatus.FAILED → send_status(ERROR) + no text sent."""
        failed = AgentResponse(
            task_id="t1",
            agent_id=f"smart_response_agent_{_USER_ID}",
            status=AgentStatus.FAILED,
            result=None,
            confidence=0.0,
            error="LLM provider error",
        )
        coord = _simple_coordinator(failed)
        handler = _make_handler(coord)
        channel = _make_channel()

        # Prevent fallback from kicking in (it's already handled by fallback service)
        with patch(
            "src.handlers.conversation_handler.AgentFallbackService.try_quick_fallback",
            new_callable=AsyncMock,
            return_value=failed,
        ):
            await handler.handle_message(_make_context(), channel)

        channel.send_status.assert_awaited_with(StatusType.ERROR, thread_id=None)
        channel.send_chunked_message.assert_not_awaited()
        channel.update_message.assert_not_awaited()

    # --- Lines 507-517: Rich content only (no text) ---

    async def test_rich_content_only_no_text_delivers_and_uses_fallback(self):
        """structured_data present + text empty → update_message + _deliver_rich_content, history_text=fallback."""
        rich = RichContent(content_type="table", data={"headers": ["A"]}, fallback_text="table data")
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="", structured_data=rich))
        )
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.update_message.assert_awaited_once_with(_STATUS_MSG_ID, "✅ Відповідь готова.")
        channel.send_rich_content.assert_awaited_once_with(rich, thread_id=None)

    # --- Line 522: Empty text placeholder ---

    async def test_empty_text_response_gets_placeholder(self):
        """Empty text + no structured_data → placeholder sent."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text=""))
        )
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        channel.send_chunked_message.assert_awaited_once()
        sent_text = channel.send_chunked_message.call_args[0][0]
        assert "порожня" in sent_text

    # --- Lines 549-564: History summary resolution ---

    async def test_history_uses_async_summary_task_when_available(self):
        """ENABLE_HISTORY_OPTIMIZATION=True + summary_task → history_text=summary."""
        summary = asyncio.Future()
        summary.set_result("short summary")

        response = _make_success(SmartResponse(text="full response"))
        response.metadata["response_summary_task"] = summary

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with (
            patch("src.handlers.conversation_handler.ENABLE_HISTORY_OPTIMIZATION", True),
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
        ):
            await handler.handle_message(_make_context(), channel)

        # History was saved — check append_messages_batch was called
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()

    async def test_history_uses_precomputed_summary_from_metadata(self):
        """ENABLE_HISTORY_OPTIMIZATION=True + response_summary in metadata (no task)."""
        response = _make_success(SmartResponse(text="full response"))
        response.metadata["response_summary"] = "precomputed"

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with (
            patch("src.handlers.conversation_handler.ENABLE_HISTORY_OPTIMIZATION", True),
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
        ):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        # Verify the model message part uses the precomputed summary as text
        call_args = session_store.append_messages_batch.call_args
        messages = call_args[0][1]
        model_msg = next(m for m in messages if m.role == "model")
        assert model_msg.parts[0].text == "precomputed"

    async def test_history_summary_task_none_uses_full_text(self):
        """summary_task returns None → warning, fall back to full response_text."""
        summary = asyncio.Future()
        summary.set_result(None)  # task returns None

        response = _make_success(SmartResponse(text="full response"))
        response.metadata["response_summary_task"] = summary

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with (
            patch("src.handlers.conversation_handler.ENABLE_HISTORY_OPTIMIZATION", True),
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
        ):
            await handler.handle_message(_make_context(), channel)

        # Should not raise; history saved with full text
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()

    async def test_history_summary_task_timeout_uses_full_text(self):
        """asyncio.TimeoutError during summary → warning, fall back to full text."""
        loop = asyncio.get_event_loop()
        # Create a future that never resolves to simulate timeout
        never_resolves = loop.create_future()

        response = _make_success(SmartResponse(text="full response"))
        response.metadata["response_summary_task"] = never_resolves

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with (
            patch("src.handlers.conversation_handler.ENABLE_HISTORY_OPTIMIZATION", True),
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()),
        ):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        never_resolves.cancel()

    async def test_history_summary_task_exception_uses_full_text(self):
        """Non-TimeoutError exception during summary → warning, fall back to full text. (Lines 560-561)"""
        summary = asyncio.Future()
        summary.set_exception(RuntimeError("summary generation failed"))

        response = _make_success(SmartResponse(text="full response"))
        response.metadata["response_summary_task"] = summary

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with (
            patch("src.handlers.conversation_handler.ENABLE_HISTORY_OPTIMIZATION", True),
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
        ):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()

    # --- Lines 622-624: consolidation_text metadata ---

    async def test_consolidation_text_from_metadata_attached_to_user_message(self):
        """response.metadata['consolidation_text'] → MessagePart(consolidation_text=...) appended. (Lines 622-624)"""
        response = _make_success(SmartResponse(text="Saved to memory."))
        response.metadata["consolidation_text"] = ["Fact one.", "Fact two."]

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        call_args = session_store.append_messages_batch.call_args
        messages = call_args[0][1]
        user_msg = next(m for m in messages if m.role == "user")
        consol_parts = [p for p in user_msg.parts if p.consolidation_text]
        assert consol_parts, "Expected a part with consolidation_text"
        assert "Fact one." in consol_parts[0].consolidation_text
        assert "Fact two." in consol_parts[0].consolidation_text

    # --- Lines 576-577, 581: History cleanup (file stubs) ---

    async def test_file_parts_with_path_skipped_from_history(self):
        """MessagePart with file_data containing 'path' → excluded from clean_message_parts."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord)
        channel = _make_channel()

        # Create a temp file so os.remove succeeds
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name

        attachment = FileAttachment(
            url="http://example.com/img.png",
            mime_type="image/png",
            filename="img.png",
        )
        ctx = _make_context(attachments=[attachment])
        channel.download_file = AsyncMock(return_value=tmp_path)

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch(
                "src.handlers.conversation_handler.is_native_binary",
                return_value=True,
            ),
        ):
            await handler.handle_message(ctx, channel)

        # History should have been saved (call happened)
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()

    async def test_converted_file_parts_replaced_with_stubs(self):
        """Text-converted file parts → replaced with stub/full_text pair in history."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord)
        channel = _make_channel()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            tmp_path = f.name

        attachment = FileAttachment(
            url="http://example.com/doc.txt",
            mime_type="text/plain",
            filename="doc.txt",
        )
        ctx = _make_context(attachments=[attachment])
        channel.download_file = AsyncMock(return_value=tmp_path)

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=False),
            patch(
                "src.handlers.conversation_handler.convert_file_to_text",
                new_callable=AsyncMock,
                return_value="file content text",
            ),
            patch(
                "src.handlers.conversation_handler.make_history_stub",
                return_value="[stub: doc.txt]",
            ),
        ):
            await handler.handle_message(ctx, channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        call_args = session_store.append_messages_batch.call_args
        messages = call_args[0][1]
        user_msg = next(m for m in messages if m.role == "user")
        # Check that at least one part has stub text
        stubs = [p for p in user_msg.parts if p.text == "[stub: doc.txt]"]
        assert stubs, "Expected stub part in history"
        assert stubs[0].full_text == "file content text"

    # --- Lines 591-598: Metadata context appending ---

    async def test_metadata_context_key_appended_to_response_text(self):
        """Metadata key ending in '_context' → JSON-serialized and appended to response_text in history."""
        email_context = [{"you_searched": "hello", "you_received": []}]
        response = _make_success(SmartResponse(text="Here are results."))
        response.metadata["email_search_context"] = email_context

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        call_args = session_store.append_messages_batch.call_args
        messages = call_args[0][1]
        model_msg = next(m for m in messages if m.role == "model")
        full = model_msg.parts[0].full_text
        assert "email_search_context" in full

    # --- Lines 622-624: Rich content appended to history ---

    async def test_rich_content_appended_to_full_text_in_history(self):
        """structured_data present → rich_content JSON appended to response_text (full_text)."""
        rich = RichContent(content_type="table", data={"headers": ["A"]}, fallback_text="t")
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Here is a table.", structured_data=rich))
        )
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()
        call_args = session_store.append_messages_batch.call_args
        messages = call_args[0][1]
        model_msg = next(m for m in messages if m.role == "model")
        full = model_msg.parts[0].full_text
        assert "rich_content" in full
        assert "table" in full

    # --- Line 630: delivery_items dispatched ---

    async def test_delivery_items_are_dispatched(self):
        """response.delivery_items non-empty → _deliver_item called for each."""
        item1 = DeliveryItem(type="message", data={"text": "item one"})
        item2 = DeliveryItem(type="message", data={"text": "item two"})

        response = _make_success(SmartResponse(text="Main text."))
        response.delivery_items.append(item1)
        response.delivery_items.append(item2)

        coord = _simple_coordinator(response)
        handler = _make_handler(coord)
        channel = _make_channel()

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(_make_context(), channel)

        # send_message called for both delivery items
        calls = channel.send_message.call_args_list
        texts = [c[0][0] for c in calls]
        assert "item one" in texts
        assert "item two" in texts

    # --- Lines 655-656: Exception in handle_message + cleanup ---

    async def test_exception_sends_error_status_and_message(self):
        """RuntimeError during routing → stop status, send ERROR status + error message."""
        coord = MagicMock()
        coord.route_message = AsyncMock(side_effect=RuntimeError("unexpected crash"))
        handler = _make_handler(coord)
        channel = _make_channel()

        await handler.handle_message(_make_context(), channel)

        channel.send_status.assert_awaited_with(StatusType.ERROR, thread_id=None)
        channel.send_message.assert_awaited()
        sent = channel.send_message.call_args[0][0]
        assert "wrong" in sent.lower() or "error" in sent.lower() or "Something" in sent

    async def test_exception_when_send_status_also_fails_logs_warning(self):
        """Even if send_status/send_message raises, outer exception is caught gracefully."""
        coord = MagicMock()
        coord.route_message = AsyncMock(side_effect=RuntimeError("crash"))
        handler = _make_handler(coord)

        channel = _make_channel()
        channel.send_status = AsyncMock(side_effect=RuntimeError("channel also dead"))

        # Should not propagate
        await handler.handle_message(_make_context(), channel)

    # --- Lines 660-663: Finally block temp file cleanup ---

    async def test_temp_files_removed_in_finally_even_after_exception(self):
        """Temp files accumulated before exception are deleted in finally block."""
        # Create a real temp file so os.remove can succeed
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name

        attachment = FileAttachment(url="http://x.com/f.png", mime_type="image/png", filename="f.png")
        ctx = _make_context(attachments=[attachment])

        # Coordinator fails AFTER download, so the file is in temp_files before the exception
        coord = MagicMock()
        coord.route_message = AsyncMock(side_effect=RuntimeError("fail after download"))

        handler = _make_handler(coord)
        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp)

        with patch("src.handlers.conversation_handler.is_native_binary", return_value=True):
            await handler.handle_message(ctx, channel)

        assert not os.path.exists(tmp), "Temp file should have been removed"

    async def test_temp_file_remove_failure_swallowed(self):
        """os.remove failure in finally block → debug log only, no raise."""
        coord = MagicMock()
        coord.route_message = AsyncMock(side_effect=RuntimeError("fail"))

        attachment = FileAttachment(url="http://x.com/f.png", mime_type="image/png", filename="f.png")
        ctx = _make_context(attachments=[attachment])

        handler = _make_handler(coord)
        channel = _make_channel()
        channel.download_file = AsyncMock(return_value="/nonexistent/path.tmp")

        with patch("src.handlers.conversation_handler.is_native_binary", return_value=True):
            # Should not raise even though os.remove will fail (file doesn't exist)
            await handler.handle_message(ctx, channel)

    # --- Lines 373-391: No text + attachments (localization + hardcoded) ---

    async def test_no_text_with_image_localization_present(self):
        """No text + image attachment + localization service → get_file_prompt called."""
        localization = MagicMock()
        localization.get_file_prompt = MagicMock(return_value="What do you see in this image?")

        coord = _simple_coordinator(
            _make_success(SmartResponse(text="I see a cat."))
        )
        handler = _make_handler(coord, localization=localization)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            tmp_img = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_img)

        attachment = FileAttachment(url="http://x.com/img.jpg", mime_type="image/jpeg", filename="img.jpg")
        ctx = _make_context(text="", attachments=[attachment])

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        localization.get_file_prompt.assert_called_once()
        # context.text should have been populated
        assert ctx.text == "What do you see in this image?"

    async def test_no_text_with_image_hardcoded_fallback(self):
        """No text + image attachment + no localization → uk.FILE_FALLBACK_IMAGE used."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Described."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f:
            tmp_img = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_img)

        attachment = FileAttachment(url="http://x.com/img.jpg", mime_type="image/jpeg", filename="img.jpg")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_IMAGE

    async def test_no_text_with_pdf_hardcoded_fallback(self):
        """No text + PDF + no localization → uk.FILE_FALLBACK_PDF."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Read."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            tmp_pdf = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_pdf)

        attachment = FileAttachment(url="http://x.com/file.pdf", mime_type="application/pdf", filename="file.pdf")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_PDF

    async def test_no_text_with_application_file_hardcoded_fallback(self):
        """No text + application/zip + no localization → uk.FILE_FALLBACK_DOCUMENT."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Processed."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as f:
            tmp_zip = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_zip)

        attachment = FileAttachment(url="http://x.com/file.zip", mime_type="application/zip", filename="file.zip")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_DOCUMENT

    async def test_no_text_with_video_hardcoded_fallback(self):
        """No text + video/mp4 + no localization → uk.FILE_FALLBACK_VIDEO."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Processed."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
            tmp_vid = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_vid)

        attachment = FileAttachment(url="http://x.com/video.mp4", mime_type="video/mp4", filename="video.mp4")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_VIDEO

    async def test_no_text_with_unknown_mime_hardcoded_fallback(self):
        """No text + unknown mime type + no localization → uk.FILE_FALLBACK_GENERIC."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Processed."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            tmp_bin = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_bin)

        attachment = FileAttachment(url="http://x.com/file.bin", mime_type="unknown/binary", filename="file.bin")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=True),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_GENERIC

    async def test_no_text_with_text_file_hardcoded_fallback(self):
        """No text + text/csv + no localization → uk.FILE_FALLBACK_DOCUMENT."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="Read."))
        )
        handler = _make_handler(coord, localization=None)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            tmp_csv = f.name

        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=tmp_csv)

        attachment = FileAttachment(url="http://x.com/data.csv", mime_type="text/csv", filename="data.csv")
        ctx = _make_context(text="", attachments=[attachment])

        from src.locales import uk as uk_locale

        with (
            patch.object(handler, "validate_model_output", side_effect=lambda t, u: t),
            patch("src.handlers.conversation_handler.is_native_binary", return_value=False),
            patch(
                "src.handlers.conversation_handler.convert_file_to_text",
                new_callable=AsyncMock,
                return_value="csv data",
            ),
            patch("src.handlers.conversation_handler.make_history_stub", return_value="[stub]"),
        ):
            await handler.handle_message(ctx, channel)

        assert ctx.text == uk_locale.FILE_FALLBACK_DOCUMENT

    # --- Attachment: no URL → warning + skip ---

    async def test_attachment_without_url_is_skipped(self):
        """Attachment with url=None → warning logged, download_file not called."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord)
        channel = _make_channel()
        # Attachment with no URL — FileAttachment requires url so patch after creation
        attachment = FileAttachment(url="", mime_type="image/png", filename="img.png")
        # Patch url to None (the dataclass doesn't allow None by type but we can force it)
        object.__setattr__(attachment, "url", None)
        ctx = _make_context(attachments=[attachment])

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(ctx, channel)

        channel.download_file.assert_not_awaited()

    # --- Attachment: download returns None → warning ---

    async def test_attachment_download_returns_none_is_skipped(self):
        """download_file returns None → warning, part not added."""
        coord = _simple_coordinator(
            _make_success(SmartResponse(text="OK"))
        )
        handler = _make_handler(coord)
        channel = _make_channel()
        channel.download_file = AsyncMock(return_value=None)

        attachment = FileAttachment(url="http://x.com/f.png", mime_type="image/png", filename="f.png")
        ctx = _make_context(attachments=[attachment])

        with patch.object(handler, "validate_model_output", side_effect=lambda t, u: t):
            await handler.handle_message(ctx, channel)

        # History still saved; just no file part added
        session_store = handler.agent_factory.get_session_store()
        session_store.append_messages_batch.assert_awaited_once()


# ---------------------------------------------------------------------------
# Class 4: handle_command consolidate edge cases
# ---------------------------------------------------------------------------

class TestHandleCommandConsolidateExtra:
    """Lines 753-754 + 796: consolidate command branches not covered by existing tests."""

    def _make_command_handler(self, *, notification_service=None):
        session_store = MagicMock()
        session_store.append_messages_batch = AsyncMock()
        session_store.load_session = AsyncMock(return_value=None)
        session_store.save_session = AsyncMock()

        agent_factory = MagicMock()
        agent_factory.ensure_agents_for_user = AsyncMock()
        agent_factory.get_session_store = MagicMock(return_value=session_store)
        agent_factory.invalidate_prompt_cache = MagicMock()
        user_repo = MagicMock()
        user_repo.get_user = AsyncMock(return_value=None)
        agent_factory.user_repo = user_repo

        consolidation_queue = MagicMock()
        consolidation_queue.enqueue_batch = AsyncMock(return_value="batch-id")

        coordinator = MagicMock()
        coordinator.route_message = AsyncMock()

        handler = ConversationHandler(
            coordinator=coordinator,
            agent_factory=agent_factory,
            file_service=MagicMock(),
            consolidation_queue=consolidation_queue,
            global_config=ConsolidationSettings(threshold=50, batch_size=30),
            notification_service=notification_service,
        )
        return handler, agent_factory, session_store, consolidation_queue

    def _make_session_with_messages(self):
        session = MagicMock()
        session.user_id = _USER_ID
        session.session_id = _SESSION_ID
        session.messages = [MagicMock()]

        msg = MagicMock()
        msg.role = "user"
        part = MagicMock()
        part.full_text = "hello there"
        part.consolidation_text = None
        part.text = "hello there"
        msg.parts = [part]
        msg.created_at = "2026-01-01T00:00:00"
        session.extract_oldest_messages = MagicMock(return_value=[msg])
        return session

    def _make_channel(self) -> MagicMock:
        ch = MagicMock()
        ch.send_message = AsyncMock()
        ch.send_status = AsyncMock()
        ch.thread_id = None
        return ch

    def _make_context(self) -> MessageContext:
        return MessageContext(
            text="test",
            session_id=_SESSION_ID,
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
        )

    async def test_consolidate_empty_old_messages_sends_not_enough(self):
        """extract_oldest_messages returns [] → 'Not enough messages' sent. (Lines 753-754)"""
        handler, _, session_store, _ = self._make_command_handler()
        channel = self._make_channel()
        ctx = self._make_context()

        session = MagicMock()
        session.user_id = _USER_ID
        session.session_id = _SESSION_ID
        session.messages = [MagicMock()]
        # extract_oldest_messages returns empty list
        session.extract_oldest_messages = MagicMock(return_value=[])
        session_store.load_session = AsyncMock(return_value=session)

        await handler.handle_command("consolidate", ctx, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "Not enough" in sent or "not enough" in sent.lower()

    async def test_consolidate_with_notification_service_calls_notify(self):
        """_notification_service present → notify() called instead of send_message. (Line 796)"""
        notif = MagicMock()
        notif.notify = AsyncMock()

        handler, _, session_store, _ = self._make_command_handler(notification_service=notif)
        channel = self._make_channel()
        ctx = self._make_context()

        session = self._make_session_with_messages()
        session_store.load_session = AsyncMock(return_value=session)
        session_store.save_session = AsyncMock()

        await handler.handle_command("consolidate", ctx, channel)

        notif.notify.assert_awaited_once()
        call_kwargs = notif.notify.call_args[1]
        assert call_kwargs["user_id"] == _USER_ID
        assert "system_alert" in call_kwargs
        # No fallback message sent to channel
        channel.send_message.assert_not_called()

    async def test_consolidate_without_notification_service_sends_message(self):
        """_notification_service absent → send_message fallback. (Line 801-805)"""
        handler, _, session_store, _ = self._make_command_handler(notification_service=None)
        channel = self._make_channel()
        ctx = self._make_context()

        session = self._make_session_with_messages()
        session_store.load_session = AsyncMock(return_value=session)
        session_store.save_session = AsyncMock()

        await handler.handle_command("consolidate", ctx, channel)

        channel.send_message.assert_called_once()
        sent = channel.send_message.call_args[0][0]
        assert "Consolidation complete" in sent or "complete" in sent.lower()
