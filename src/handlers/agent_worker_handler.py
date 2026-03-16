"""
Agent Worker Handler
====================

Handles async agent task execution triggered by Cloud Tasks.
Receives payloads with task_type="agent_execution", executes the
specified agent, and handles result delivery.

Deep research delivery:
  When intent == "execute_deep_research_claude", the runner agent returns
  the research text in AgentResponse.result. This handler uploads round
  markdown files to GCS, sends named links to the user, and enqueues an
  HtmlPageGenerator Cloud Task to produce the final styled report.
"""

import base64
from typing import Dict, Any, Optional

from ..domain.agent import AgentMessage, AgentIntent, AgentStatus
from ..services.deep_research_delivery import (
    NotificationPort, deliver_deep_research,
)
from ..services.document_delivery_service import DocumentDeliveryService
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.agent_manifest import Intent
from ..ports.media_storage_port import MediaStoragePort
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger


class AgentWorkerHandler:
    """
    Background task executor for async agent intents.

    Invoked by the /worker HTTP endpoint when Cloud Tasks delivers a
    payload with task_type="agent_execution".

    Responsibilities:
    - Resolve agent instance from coordinator
    - Execute agent with the original query + context
    - Deliver result via notification service (SmartAgent summary + report link)
    - Log result (success or failure)
    """

    def __init__(
        self,
        coordinator: AgentCoordinator,
        notification_service: Optional[NotificationPort] = None,
        media_storage: Optional[MediaStoragePort] = None,
        task_queue: Optional[TaskQueue] = None,
        doc_delivery_service: Optional[DocumentDeliveryService] = None,
    ) -> None:
        self._coordinator = coordinator
        self._notification = notification_service
        self._media_storage = media_storage
        self._task_queue = task_queue
        self._doc_delivery_service = doc_delivery_service

    async def handle_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute an async agent task.

        Expected payload shape:
        {
            "task_type": "agent_execution",
            "agent_id": "claude_deep_research_runner",
            "intent":   "execute_deep_research_claude",
            "query":    "...",
            "context":  {"user_id": "...", "account_id": "...", ...}
        }

        Returns a result dict (used by the HTTP endpoint for the response body).
        """
        agent_id = payload.get("agent_id", "unknown")
        intent = payload.get("intent", "unknown")
        query = payload.get("query", "")
        context = payload.get("context", {})

        user_id = context.get("user_id", "")
        resolved_agent_id = f"{agent_id}_{user_id}" if user_id else agent_id

        logger.info(
            f"[AgentWorkerHandler] Executing: agent={resolved_agent_id}, "
            f"intent={intent}, user={user_id}"
        )

        # Mirror _execute_sync: spread context["params"] into payload so agents
        # receive extra fields (e.g. report_content) via message.payload.
        extra_payload = context.get("params", {})
        message = AgentMessage.create(
            sender="worker",
            recipient=resolved_agent_id,
            intent=AgentIntent.DELEGATE,
            payload={"query": query, "intent": intent, **extra_payload},
            context={k: v for k, v in context.items() if k != "params"},
        )

        try:
            response = await self._coordinator.route_message(message)

            if response.status == AgentStatus.SUCCESS:
                logger.info(
                    f"[AgentWorkerHandler] Task completed: agent={resolved_agent_id}, "
                    f"intent={intent}"
                )
                # Deep research delivery — runner returns result text, we deliver it.
                if intent == Intent.EXECUTE_DEEP_RESEARCH_CLAUDE:
                    await self._deliver_deep_research_result(response, context)
                # DOCX delivery — generator runs as its own Cloud Task and delivers directly.
                elif intent in (Intent.CREATE_DOCUMENT, Intent.GENERATE_DOCX_CODE):
                    await self._deliver_docx_result(response, context)
                # PDF / HTML page delivery — generator produces "document" DeliveryItems.
                elif intent in (Intent.CREATE_PDF, Intent.CREATE_HTML_PAGE):
                    await self._deliver_document_result(response, context)

                return {"status": "success", "agent_id": resolved_agent_id, "intent": intent}

            else:
                logger.error(
                    f"[AgentWorkerHandler] Task failed: agent={resolved_agent_id}, "
                    f"intent={intent}, status={response.status}, error={response.error}"
                )
                # Notify user of deep research failure.
                if intent == Intent.EXECUTE_DEEP_RESEARCH_CLAUDE:
                    await self._notify_failure(context)
                elif intent in (Intent.CREATE_DOCUMENT, Intent.GENERATE_DOCX_CODE):
                    await self._notify_docx_failure(context, response.error)
                elif intent in (Intent.CREATE_PDF, Intent.CREATE_HTML_PAGE):
                    await self._notify_docx_failure(context, response.error)

                return {
                    "status": "failed",
                    "agent_id": resolved_agent_id,
                    "intent": intent,
                    "error": response.error,
                }

        except Exception as e:
            logger.error(
                f"[AgentWorkerHandler] Unexpected error: agent={resolved_agent_id}, "
                f"intent={intent}, error={e}",
                exc_info=True,
            )
            # Notify user of deep research failure on unexpected errors.
            if intent == Intent.EXECUTE_DEEP_RESEARCH_CLAUDE:
                await self._notify_failure(context)
            raise

    async def _deliver_deep_research_result(
        self, response: Any, context: Dict[str, Any]
    ) -> None:
        """Upload round markdown files to GCS, send named links, enqueue HtmlPageGenerator task."""

        result = response.result
        if not isinstance(result, dict) or not result.get("text"):
            logger.warning("[AgentWorkerHandler] Deep research result has no text — skipping delivery")
            return

        await deliver_deep_research(
            result_text=result["text"],
            round1_text=result.get("round1_text", ""),
            user_id=context.get("user_id", ""),
            account_id=context.get("account_id", ""),
            query=result.get("query", context.get("original_query", "")),
            task_queue=self._task_queue,
            session_id=context.get("session_id", ""),
            media_storage=self._media_storage,
            notification=self._notification,
        )

    async def _deliver_docx_result(self, response: Any, context: Dict[str, Any]) -> None:
        """Upload DOCX file(s) from delivery_items to the user's last active channel."""
        if not self._notification:
            logger.warning("[AgentWorkerHandler] No notification service configured for DOCX delivery")
            return

        user_id = context.get("user_id", "")
        account_id = context.get("account_id", "")

        for item in getattr(response, "delivery_items", []):
            if item.type != "file_upload":
                continue
            try:
                file_bytes = base64.b64decode(item.data["file_bytes_b64"])
                await self._notification.notify_file_bytes(
                    user_id=user_id,
                    account_id=account_id,
                    file_bytes=file_bytes,
                    filename=item.data.get("filename", "document.docx"),
                    title=item.data.get("title", "Document"),
                )
            except Exception as exc:
                logger.error(
                    "[AgentWorkerHandler] DOCX delivery failed for user=%s: %s",
                    user_id[:8], exc, exc_info=True,
                )

    async def _deliver_document_result(self, response: Any, context: Dict[str, Any]) -> None:
        """Deliver document DeliveryItems (PDF/HTML) from async generator tasks."""
        if not self._notification:
            logger.warning("[AgentWorkerHandler] No notification service configured for document delivery")
            return
        if not self._doc_delivery_service:
            logger.warning("[AgentWorkerHandler] No DocumentDeliveryService configured for document delivery")
            return

        user_id = context.get("user_id", "")
        account_id = context.get("account_id", "")

        for item in getattr(response, "delivery_items", []):
            if item.type != "document":
                continue
            try:
                content = base64.b64decode(item.data["content_b64"])
                filename = item.data["filename"]
                label = item.data.get("label", filename)
                url = await self._doc_delivery_service.store(
                    content, filename, item.data["content_type"]
                )
                await self._notification.notify_document_link(
                    user_id=user_id, account_id=account_id, url=url, label=label
                )
                if item.data.get("file_upload"):
                    await self._notification.notify_file_bytes(
                        user_id=user_id,
                        account_id=account_id,
                        file_bytes=content,
                        filename=filename,
                        title=label,
                    )
            except Exception as exc:
                logger.error(
                    "[AgentWorkerHandler] Document delivery failed for user=%s: %s",
                    user_id[:8], exc, exc_info=True,
                )

    async def _notify_docx_failure(self, context: Dict[str, Any], error: Optional[str]) -> None:
        """Notify user that document creation failed."""
        if not self._notification:
            return
        await self._notification.notify(
            user_id=context.get("user_id", ""),
            account_id=context.get("account_id", ""),
            system_alert=(
                f"Document creation did not complete — {error or 'an error occurred'}."
            ),
        )

    async def _notify_failure(self, context: Dict[str, Any]) -> None:
        """Notify user that deep research failed."""
        if not self._notification:
            return

        await self._notification.notify(
            user_id=context.get("user_id", ""),
            account_id=context.get("account_id", ""),
            system_alert=(
                "Deep research did not complete — "
                "the Claude research loop encountered an error."
            ),
        )
