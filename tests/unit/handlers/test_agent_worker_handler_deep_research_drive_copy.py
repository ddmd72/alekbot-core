"""The deep research report's drive copy at delivery (decisions/deep_research_drive_copy.md).

Every deep research entry point (the Claude runner via agent_execution or the Cloud Run Job, OpenAI's
webhook via its worker task, the polling path) funnels into `deliver_deep_research`, which marks the HtmlPageGenerator task's
context with the research question. When that task's HTML is delivered, AgentWorkerHandler sends the link
first and then asks the copy service for the drive copy; its one line follows the link. The copy never
fails the delivery.
"""
import base64
import json
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.domain.agent import AgentResponse, DeliveryItem
from src.domain.deep_research_drive_copy import DEEP_RESEARCH_REPORT_CONTEXT_KEY
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.agent_manifest import Intent
from src.services.deep_research_delivery import deliver_deep_research
from src.services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from src.services.document_delivery_service import DeliveredDocument
from src.web.deep_research_webhooks import create_deep_research_webhooks_blueprint

USER = "user-abc"
ACCOUNT = "acc-abc"
HTML = b"<!DOCTYPE html><html><head><title>Heat pumps</title></head><body>report</body></html>"
LINE = "📁 Copied to your drive: Deep research/2026-10-14 Heat pumps.html"


def _html_item(content: bytes = HTML) -> DeliveryItem:
    return DeliveryItem(type="document", data={
        "content_b64": base64.b64encode(content).decode(),
        "filename": "heat_pumps.html",
        "content_type": "text/html; charset=utf-8",
        "label": "Heat pumps.html",
        "file_upload": False,
        "storage_class": "document",
    })


def _handler(copy=None, with_copy=True):
    coordinator = AsyncMock()
    notification = AsyncMock()
    doc_delivery = AsyncMock()
    doc_delivery.store.return_value = DeliveredDocument(link="https://x/f/t", key="docs/u/uuid-heat_pumps.html")
    if with_copy and copy is None:
        copy = AsyncMock(spec=DeepResearchDriveCopyService)
        copy.copy_report.return_value = LINE
    handler = AgentWorkerHandler(
        coordinator=coordinator, notification_service=notification, task_queue=AsyncMock(),
        doc_delivery_service=doc_delivery, deep_research_drive_copy=copy if with_copy else None,
    )
    return handler, coordinator, notification, copy


def _html_payload(**extra_ctx) -> dict:
    return {
        "task_type": "agent_execution", "agent_id": "html_page_generator_agent",
        "intent": Intent.CREATE_HTML_PAGE, "query": "Create an HTML page…",
        "context": {"user_id": USER, "account_id": ACCOUNT, "session_id": f"{USER}:C1",
                    "origin_channel_id": "C1", **extra_ctx},
    }


def _success(items) -> AgentResponse:
    return AgentResponse.success(task_id="t", agent_id="a", result="html_page_generated", delivery_items=items)


class TestDeliveryCopies:
    async def test_link_first_then_the_copy_line(self):
        handler, coordinator, notification, copy = _handler()
        coordinator.route_message.return_value = _success([_html_item()])
        order = []
        notification.notify_document_link.side_effect = lambda **kw: order.append("link")
        notification.notify_raw.side_effect = lambda *a, **kw: order.append("line")

        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "heat pumps?"}))

        assert result["status"] == "success"
        copy.copy_report.assert_awaited_once_with(USER, HTML, "heat pumps?")
        assert order == ["link", "line"]
        args, kwargs = notification.notify_raw.call_args
        assert LINE in args or kwargs.get("text") == LINE
        assert kwargs.get("channel_id_override") == "C1"

    async def test_an_ordinary_html_page_is_not_copied(self):
        handler, coordinator, notification, copy = _handler()
        coordinator.route_message.return_value = _success([_html_item()])
        await handler.handle_task(_html_payload())
        copy.copy_report.assert_not_called()
        notification.notify_raw.assert_not_called()
        notification.notify_document_link.assert_awaited_once()

    async def test_toggle_off_posts_no_line(self):
        copy = AsyncMock(spec=DeepResearchDriveCopyService)
        copy.copy_report.return_value = None
        handler, coordinator, notification, _ = _handler(copy=copy)
        coordinator.route_message.return_value = _success([_html_item()])
        await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        notification.notify_raw.assert_not_called()
        notification.notify_document_link.assert_awaited_once()

    async def test_no_copy_service_wired_still_delivers(self):
        handler, coordinator, notification, _ = _handler(with_copy=False)
        coordinator.route_message.return_value = _success([_html_item()])
        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        assert result["status"] == "success"
        notification.notify_document_link.assert_awaited_once()
        notification.notify_raw.assert_not_called()

    async def test_a_non_html_document_is_not_copied(self):
        handler, coordinator, _, copy = _handler()
        pdf = DeliveryItem(type="document", data={"content_b64": base64.b64encode(b"%PDF").decode(),
                                                  "filename": "r.pdf", "content_type": "application/pdf"})
        coordinator.route_message.return_value = _success([pdf])
        await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        copy.copy_report.assert_not_called()


class TestDeliveryNeverFailsBecauseOfTheCopy:
    async def test_copy_raises(self):
        copy = AsyncMock(spec=DeepResearchDriveCopyService)
        copy.copy_report.side_effect = RuntimeError("unexpected")
        handler, coordinator, notification, _ = _handler(copy=copy)
        coordinator.route_message.return_value = _success([_html_item()])
        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        assert result["status"] == "success"
        notification.notify_document_link.assert_awaited_once()

    async def test_the_line_fails_to_post(self):
        handler, coordinator, notification, _ = _handler()
        notification.notify_raw.side_effect = RuntimeError("slack down")
        coordinator.route_message.return_value = _success([_html_item()])
        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        assert result["status"] == "success"

    async def test_the_link_failing_skips_the_copy(self):
        """No report reached the user: nothing to copy (the copy follows a delivered report only)."""
        handler, coordinator, notification, copy = _handler()
        notification.notify_document_link.side_effect = RuntimeError("slack down")
        coordinator.route_message.return_value = _success([_html_item()])
        result = await handler.handle_task(_html_payload(**{DEEP_RESEARCH_REPORT_CONTEXT_KEY: "q"}))
        assert result["status"] == "success"
        copy.copy_report.assert_not_called()


class TestMarker:
    async def test_deliver_deep_research_marks_the_html_task_with_the_question(self):
        queue = AsyncMock()
        await deliver_deep_research(result_text="findings", user_id=USER, account_id=ACCOUNT,
                                    query="Heat pumps in Springfield?", task_queue=queue)
        ctx = queue.enqueue_agent_task.call_args.kwargs["context"]
        assert ctx[DEEP_RESEARCH_REPORT_CONTEXT_KEY] == "Heat pumps in Springfield?"

    async def test_a_huge_question_is_bounded_in_the_marker(self):
        queue = AsyncMock()
        await deliver_deep_research(result_text="findings", user_id=USER, account_id=ACCOUNT,
                                    query="q" * 10_000, task_queue=queue)
        marker = queue.enqueue_agent_task.call_args.kwargs["context"][DEEP_RESEARCH_REPORT_CONTEXT_KEY]
        assert 0 < len(marker) <= 500

    async def test_an_empty_question_still_marks_the_task(self):
        queue = AsyncMock()
        await deliver_deep_research(result_text="findings", user_id=USER, account_id=ACCOUNT,
                                    query="", task_queue=queue)
        ctx = queue.enqueue_agent_task.call_args.kwargs["context"]
        assert DEEP_RESEARCH_REPORT_CONTEXT_KEY in ctx


async def _replay_html_task(enqueued_kwargs: dict) -> AsyncMock:
    """Run the enqueued HtmlPageGenerator task through the worker handler; return the copy service."""
    handler, coordinator, _, copy = _handler()
    coordinator.route_message.return_value = _success([_html_item()])
    await handler.handle_task({"task_type": "agent_execution", "agent_id": enqueued_kwargs["agent_id"],
                               "intent": enqueued_kwargs["intent"], "query": enqueued_kwargs["query"],
                               "context": enqueued_kwargs["context"]})
    return copy


class TestEveryDeliveryPathCopies:
    async def test_claude_runner_path(self):
        """agent_execution `execute_deep_research_claude` (also what the Cloud Run Job reaches via
        deliver_deep_research) → HtmlPageGenerator task → copy."""
        queue = AsyncMock()
        handler = AgentWorkerHandler(coordinator=AsyncMock(), notification_service=AsyncMock(), task_queue=queue)
        handler._coordinator.route_message.return_value = AgentResponse.success(
            task_id="t", agent_id="a", result={"text": "findings", "query": "Heat pumps?"})
        await handler.handle_task({"task_type": "agent_execution", "agent_id": "claude_deep_research_runner",
                                   "intent": Intent.EXECUTE_DEEP_RESEARCH_CLAUDE, "query": "Heat pumps?",
                                   "context": {"user_id": USER, "account_id": ACCOUNT}})
        copy = await _replay_html_task(queue.enqueue_agent_task.call_args.kwargs)
        copy.copy_report.assert_awaited_once_with(USER, HTML, "Heat pumps?")

    async def test_openai_webhook_path(self):
        """OpenAI webhook → its `deep_research_webhook_event` worker task (S1b W) → deliver_deep_research
        → HtmlPageGenerator task → copy."""
        webhook_queue = AsyncMock()
        app = Quart("t")
        app.register_blueprint(create_deep_research_webhooks_blueprint(webhook_secret=None, task_queue=webhook_queue))
        payload = {"type": "response.completed", "data": {
            "id": "resp_1", "output_text": "findings",
            "metadata": {"user_id": USER, "account_id": ACCOUNT, "query": "What is X?",
                         "session_id": f"{USER}:C1"}}}
        async with app.test_client() as client:
            resp = await client.post("/webhooks/openai/deep-research", data=json.dumps(payload),
                                     headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        task = webhook_queue.enqueue_worker_task.await_args.kwargs

        queue = AsyncMock()
        worker = WorkerHandler(
            agent_worker_handler=AsyncMock(), email_indexing_service=MagicMock(),
            notification_service=AsyncMock(), consolidation_service=None,
            coordinator=MagicMock(), agent_factory=AsyncMock(),
            indexed_email_repo=None, user_repo=MagicMock(), task_dispatch=queue, media_storage=None,
            deep_research_fetch=AsyncMock(return_value={"output_text": "findings", "model": "m"}),
        )
        _, status = await worker._dispatch({"task_type": task["task_type"], **task["payload"]}, {})
        assert status == 200
        copy = await _replay_html_task(queue.enqueue_agent_task.call_args.kwargs)
        copy.copy_report.assert_awaited_once_with(USER, HTML, "What is X?")
