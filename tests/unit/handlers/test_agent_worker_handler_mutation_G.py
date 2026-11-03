"""Mutation-survivor kills for AgentWorkerHandler: the forwarded message payload, per-item delivery
isolation, and the logged timeout of the drive copy."""
import asyncio
import base64
from unittest.mock import AsyncMock, patch

import src.handlers.agent_worker_handler as worker_module
from src.domain.agent import DeliveryItem
from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY
from src.infrastructure.agent_manifest import Intent
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService

from tests.unit.handlers.test_agent_worker_handler import (
    _CONTEXT, _make_handler, _payload, _success_response,
)
from tests.unit.handlers.test_agent_worker_handler_deep_research_drive_copy import (
    _handler, _html_item, _html_payload, _success,
)


async def test_the_routed_message_carries_query_and_intent_beside_the_params():
    handler, coordinator, _, _ = _make_handler()
    coordinator.route_message.return_value = _success_response()
    payload = _payload("some_intent", params={"extra": "x"})

    await handler.handle_task(payload)

    message = coordinator.route_message.await_args.args[0]
    assert message.payload == {"query": "test query", "intent": "some_intent", "extra": "x"}


def _docx_item(raw: bytes, name: str) -> DeliveryItem:
    return DeliveryItem(type="file_upload", data={
        "file_bytes_b64": base64.b64encode(raw).decode(), "filename": name, "title": name})


async def test_a_failing_docx_upload_does_not_stop_the_next_item():
    handler, _, notification, _ = _make_handler()
    notification.notify_file_bytes.side_effect = [RuntimeError("boom"), None]
    response = _success_response(delivery_items=[_docx_item(b"a", "a.docx"), _docx_item(b"b", "b.docx")])

    await handler._deliver_docx_result(response, _CONTEXT)

    assert [c.kwargs["filename"] for c in notification.notify_file_bytes.await_args_list] == ["a.docx", "b.docx"]


async def test_a_failing_document_item_does_not_stop_the_next_item():
    handler, _, notification, doc_delivery = _make_handler()

    def item(name: str) -> DeliveryItem:
        return DeliveryItem(type="document", data={
            "content_b64": base64.b64encode(b"x").decode(), "filename": name,
            "content_type": "application/pdf"})

    notification.notify_document_link.side_effect = [RuntimeError("boom"), None]
    response = _success_response(delivery_items=[item("a.pdf"), item("b.pdf")])

    await handler._deliver_document_result(response, _CONTEXT)

    assert [c.args[1] for c in doc_delivery.store.await_args_list] == ["a.pdf", "b.pdf"]
    assert notification.notify_document_link.await_count == 2


async def test_the_drive_copy_timeout_is_logged_as_a_warning(monkeypatch):
    monkeypatch.setattr(worker_module, "DEEP_RESEARCH_COPY_TIMEOUT_S", 0.05)
    copy = AsyncMock(spec=DeepResearchDriveCopyService)

    async def slow(*_a, **_k):
        await asyncio.sleep(5)

    copy.copy_report.side_effect = slow
    handler, coordinator, _, _ = _handler(copy=copy)
    coordinator.route_message.return_value = _success([_html_item()])

    with patch.object(worker_module, "logger") as log:
        await asyncio.wait_for(
            handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"})), timeout=2)

    assert any("timed out" in str(c.args[0]) for c in log.warning.call_args_list)
