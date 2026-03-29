"""
Unit tests for AgentWorkerHandler.

Coverage:
  handle_task
    - SUCCESS generic intent      → returns success, no delivery
    - SUCCESS deep research        → deliver_deep_research called
    - SUCCESS create_document      → _deliver_docx_result called
    - SUCCESS generate_docx_code   → _deliver_docx_result called
    - SUCCESS create_pdf           → _deliver_document_result called
    - SUCCESS create_html_page     → _deliver_document_result called
    - FAILED deep research         → _notify_failure called
    - FAILED create_document       → _notify_docx_failure called
    - FAILED generic intent        → returns failed, no notification
    - Exception + deep research    → _notify_failure + re-raise
    - Exception + generic          → re-raise, no notify

  _deliver_docx_result
    - No notification service      → skips silently
    - file_upload item present     → decodes bytes + calls notify_file_bytes
    - non-file_upload item skipped → notify_file_bytes not called

  _deliver_document_result
    - No notification service      → skips silently
    - No doc_delivery_service      → skips silently
    - document item                → stores + notify_document_link
    - document item with file_upload flag → also calls notify_file_bytes

  _notify_failure / _notify_docx_failure
    - No notification service      → no-op
    - Has notification service     → calls notify with correct alert
"""
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.agent import AgentResponse, AgentStatus, DeliveryItem
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.infrastructure.agent_manifest import Intent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID   = "user-abc"
_ACCOUNT_ID = "acc-abc"

_CONTEXT = {
    "user_id": _USER_ID,
    "account_id": _ACCOUNT_ID,
    "session_id": "sess-001",
    "original_query": "Research AI trends",
}


def _make_handler(*, with_notification: bool = True, with_doc_delivery: bool = True):
    coordinator = MagicMock()
    coordinator.route_message = AsyncMock()

    notification = AsyncMock() if with_notification else None
    if notification:
        notification.notify = AsyncMock()
        notification.notify_file_bytes = AsyncMock()
        notification.notify_document_link = AsyncMock()

    doc_delivery = AsyncMock() if with_doc_delivery else None
    if doc_delivery:
        doc_delivery.store = AsyncMock(return_value="https://example.com/doc.pdf")

    task_queue = AsyncMock()

    handler = AgentWorkerHandler(
        coordinator=coordinator,
        notification_service=notification,
        task_queue=task_queue,
        doc_delivery_service=doc_delivery,
    )
    return handler, coordinator, notification, doc_delivery


def _success_response(intent: str = "generic", delivery_items=None) -> AgentResponse:
    return AgentResponse.success(
        task_id="task-1",
        agent_id="agent-1",
        result=None,
        delivery_items=delivery_items or [],
    )


def _failed_response(error: str = "provider error") -> AgentResponse:
    return AgentResponse(
        task_id="task-1",
        agent_id="agent-1",
        status=AgentStatus.FAILED,
        result=None,
        confidence=0.0,
        error=error,
    )


def _payload(intent: str, **ctx_overrides) -> dict:
    ctx = {**_CONTEXT, **ctx_overrides}
    return {
        "task_type": "agent_execution",
        "agent_id": "some_agent",
        "intent": intent,
        "query": "test query",
        "context": ctx,
    }


# ---------------------------------------------------------------------------
# handle_task
# ---------------------------------------------------------------------------

class TestHandleTask:

    async def test_success_generic_intent_returns_success(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = _success_response()

        result = await handler.handle_task(_payload("generic_intent"))

        assert result["status"] == "success"

    async def test_success_generic_intent_no_delivery_called(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = _success_response()

        await handler.handle_task(_payload("generic_intent"))

        notification.notify.assert_not_called()
        notification.notify_file_bytes.assert_not_called()
        notification.notify_document_link.assert_not_called()

    async def test_success_deep_research_calls_deliver_deep_research(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = AgentResponse.success(
            task_id="t1",
            agent_id="a1",
            result={"text": "research output", "query": "AI trends"},
        )

        with patch(
            "src.handlers.agent_worker_handler.deliver_deep_research",
            new_callable=AsyncMock,
        ) as mock_deliver:
            result = await handler.handle_task(_payload(Intent.EXECUTE_DEEP_RESEARCH_CLAUDE))

        assert result["status"] == "success"
        mock_deliver.assert_called_once()

    async def test_success_create_document_calls_deliver_docx(self):
        handler, coordinator, notification, _ = _make_handler()
        file_bytes_b64 = base64.b64encode(b"%PDF test").decode()
        item = DeliveryItem(type="file_upload", data={
            "file_bytes_b64": file_bytes_b64,
            "filename": "report.docx",
            "title": "Report",
        })
        coordinator.route_message.return_value = _success_response(
            delivery_items=[item]
        )

        await handler.handle_task(_payload(Intent.CREATE_DOCUMENT))

        notification.notify_file_bytes.assert_called_once()

    async def test_success_generate_docx_code_calls_deliver_docx(self):
        handler, coordinator, notification, _ = _make_handler()
        file_bytes_b64 = base64.b64encode(b"docx data").decode()
        item = DeliveryItem(type="file_upload", data={
            "file_bytes_b64": file_bytes_b64,
            "filename": "out.docx",
            "title": "Out",
        })
        coordinator.route_message.return_value = _success_response(delivery_items=[item])

        await handler.handle_task(_payload(Intent.GENERATE_DOCX_CODE))

        notification.notify_file_bytes.assert_called_once()

    async def test_success_create_pdf_calls_deliver_document(self):
        handler, coordinator, notification, doc_delivery = _make_handler()
        content_b64 = base64.b64encode(b"%PDF-1.4").decode()
        item = DeliveryItem(type="document", data={
            "content_b64": content_b64,
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "label": "Report",
        })
        coordinator.route_message.return_value = _success_response(delivery_items=[item])

        await handler.handle_task(_payload(Intent.CREATE_PDF))

        doc_delivery.store.assert_called_once()
        notification.notify_document_link.assert_called_once()

    async def test_success_create_html_page_calls_deliver_document(self):
        handler, coordinator, notification, doc_delivery = _make_handler()
        content_b64 = base64.b64encode(b"<html></html>").decode()
        item = DeliveryItem(type="document", data={
            "content_b64": content_b64,
            "filename": "page.html",
            "content_type": "text/html",
            "label": "Page",
        })
        coordinator.route_message.return_value = _success_response(delivery_items=[item])

        await handler.handle_task(_payload(Intent.CREATE_HTML_PAGE))

        doc_delivery.store.assert_called_once()
        notification.notify_document_link.assert_called_once()

    async def test_failed_deep_research_calls_notify_failure(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = _failed_response()

        result = await handler.handle_task(_payload(Intent.EXECUTE_DEEP_RESEARCH_CLAUDE))

        assert result["status"] == "failed"
        notification.notify.assert_called_once()
        alert = notification.notify.call_args.kwargs["system_alert"]
        assert "Deep research" in alert

    async def test_failed_create_document_calls_notify_docx_failure(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = _failed_response("generation error")

        result = await handler.handle_task(_payload(Intent.CREATE_DOCUMENT))

        assert result["status"] == "failed"
        notification.notify.assert_called_once()
        alert = notification.notify.call_args.kwargs["system_alert"]
        assert "generation error" in alert

    async def test_failed_generic_intent_no_notification(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.return_value = _failed_response()

        result = await handler.handle_task(_payload("generic_intent"))

        assert result["status"] == "failed"
        notification.notify.assert_not_called()

    async def test_exception_deep_research_notifies_and_reraises(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.side_effect = RuntimeError("API down")

        with pytest.raises(RuntimeError):
            await handler.handle_task(_payload(Intent.EXECUTE_DEEP_RESEARCH_CLAUDE))

        notification.notify.assert_called_once()

    async def test_exception_generic_reraises_without_notify(self):
        handler, coordinator, notification, _ = _make_handler()
        coordinator.route_message.side_effect = RuntimeError("timeout")

        with pytest.raises(RuntimeError):
            await handler.handle_task(_payload("generic_intent"))

        notification.notify.assert_not_called()


# ---------------------------------------------------------------------------
# _deliver_docx_result
# ---------------------------------------------------------------------------

class TestDeliverDocxResult:

    async def test_no_notification_service_skips(self):
        handler, _, _, _ = _make_handler(with_notification=False)
        response = _success_response()
        # Should complete without error
        await handler._deliver_docx_result(response, _CONTEXT)

    async def test_non_file_upload_item_skipped(self):
        handler, _, notification, _ = _make_handler()
        response = _success_response(delivery_items=[
            DeliveryItem(type="rich_content", data={"content_type": "table"})
        ])

        await handler._deliver_docx_result(response, _CONTEXT)

        notification.notify_file_bytes.assert_not_called()

    async def test_file_upload_item_decoded_and_delivered(self):
        handler, _, notification, _ = _make_handler()
        raw = b"PK\x03\x04docx bytes"
        file_bytes_b64 = base64.b64encode(raw).decode()
        response = _success_response(delivery_items=[
            DeliveryItem(type="file_upload", data={
                "file_bytes_b64": file_bytes_b64,
                "filename": "doc.docx",
                "title": "My Doc",
            })
        ])

        await handler._deliver_docx_result(response, _CONTEXT)

        notification.notify_file_bytes.assert_called_once_with(
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
            file_bytes=raw,
            filename="doc.docx",
            title="My Doc",
        )


# ---------------------------------------------------------------------------
# _deliver_document_result
# ---------------------------------------------------------------------------

class TestDeliverDocumentResult:

    async def test_no_notification_service_skips(self):
        handler, _, _, _ = _make_handler(with_notification=False)
        response = _success_response()
        await handler._deliver_document_result(response, _CONTEXT)

    async def test_no_doc_delivery_service_skips(self):
        handler, _, notification, _ = _make_handler(with_doc_delivery=False)
        response = _success_response()
        await handler._deliver_document_result(response, _CONTEXT)
        notification.notify_document_link.assert_not_called()

    async def test_document_item_stored_and_linked(self):
        handler, _, notification, doc_delivery = _make_handler()
        raw = b"%PDF-1.4"
        content_b64 = base64.b64encode(raw).decode()
        response = _success_response(delivery_items=[
            DeliveryItem(type="document", data={
                "content_b64": content_b64,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "label": "Annual Report",
            })
        ])

        await handler._deliver_document_result(response, _CONTEXT)

        doc_delivery.store.assert_called_once_with(raw, "report.pdf", "application/pdf")
        notification.notify_document_link.assert_called_once_with(
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
            url="https://example.com/doc.pdf",
            label="Annual Report",
        )
        notification.notify_file_bytes.assert_not_called()

    async def test_document_item_with_file_upload_flag_also_sends_bytes(self):
        handler, _, notification, doc_delivery = _make_handler()
        raw = b"%PDF-1.4 data"
        content_b64 = base64.b64encode(raw).decode()
        response = _success_response(delivery_items=[
            DeliveryItem(type="document", data={
                "content_b64": content_b64,
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "label": "Report",
                "file_upload": True,
            })
        ])

        await handler._deliver_document_result(response, _CONTEXT)

        notification.notify_document_link.assert_called_once()
        notification.notify_file_bytes.assert_called_once_with(
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
            file_bytes=raw,
            filename="report.pdf",
            title="Report",
        )

    async def test_non_document_item_skipped(self):
        handler, _, notification, doc_delivery = _make_handler()
        response = _success_response(delivery_items=[
            DeliveryItem(type="file_upload", data={"file_bytes_b64": "", "filename": "f.docx"})
        ])

        await handler._deliver_document_result(response, _CONTEXT)

        doc_delivery.store.assert_not_called()
        notification.notify_document_link.assert_not_called()


# ---------------------------------------------------------------------------
# _notify_failure / _notify_docx_failure
# ---------------------------------------------------------------------------

class TestNotifyMethods:

    async def test_notify_failure_no_notification_is_noop(self):
        handler, _, _, _ = _make_handler(with_notification=False)
        # Should not raise
        await handler._notify_failure(_CONTEXT)

    async def test_notify_failure_sends_deep_research_alert(self):
        handler, _, notification, _ = _make_handler()

        await handler._notify_failure(_CONTEXT)

        notification.notify.assert_called_once_with(
            user_id=_USER_ID,
            account_id=_ACCOUNT_ID,
            system_alert=pytest.approx("Deep research did not complete — "
                                        "the Claude research loop encountered an error.",
                                       abs=0),
        )

    async def test_notify_docx_failure_no_notification_is_noop(self):
        handler, _, _, _ = _make_handler(with_notification=False)
        await handler._notify_docx_failure(_CONTEXT, "render failed")

    async def test_notify_docx_failure_includes_error_in_alert(self):
        handler, _, notification, _ = _make_handler()

        await handler._notify_docx_failure(_CONTEXT, "render failed")

        notification.notify.assert_called_once()
        alert = notification.notify.call_args.kwargs["system_alert"]
        assert "render failed" in alert
