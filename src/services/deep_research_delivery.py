"""
Deep Research Delivery Helpers
==============================

Shared by WorkerHandler, AgentWorkerHandler, and deep_research_webhooks.

- upload_html_report()    — wrap markdown in HTML, upload to GCS via MediaStoragePort
                            (kept for debugging; not called from deliver_deep_research)
- deliver_deep_research() — enqueue DocPlanner Cloud Task → DocGenerator → DOCX file to user
- NotificationPort        — structural Protocol for UserNotificationService
"""

import html as html_lib
from datetime import datetime
from typing import Optional, Protocol

from ..ports.media_storage_port import MediaStoragePort
from ..ports.task_queue import TaskQueue
from ..utils.logger import logger


class NotificationPort(Protocol):
    """Protocol for user notification. Implemented by UserNotificationService."""

    async def notify(
        self,
        user_id: str,
        account_id: str,
        system_alert: str,
        agent_id_override: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> None: ...

    async def notify_raw(
        self,
        user_id: str,
        account_id: str,
        text: str,
    ) -> None: ...

    async def notify_file_bytes(
        self,
        user_id: str,
        account_id: str,
        file_bytes: bytes,
        filename: str,
        title: str,
    ) -> None: ...

    async def notify_document_link(
        self,
        user_id: str,
        account_id: str,
        url: str,
        label: str,
    ) -> None: ...


async def upload_html_report(
    markdown_text: str,
    user_id: str,
    media_storage: Optional[MediaStoragePort],
) -> Optional[str]:
    """Wrap markdown in HTML and upload to GCS. Returns public URL or None.

    Kept for debugging purposes. Not called from deliver_deep_research().
    """
    if not media_storage:
        return None
    try:
        escaped = html_lib.escape(markdown_text)
        html_page = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Deep Research Report</title>"
            "<style>body{max-width:860px;margin:40px auto;padding:0 20px 60px;"
            "font-family:Georgia,'Times New Roman',serif;font-size:17px;"
            "line-height:1.75;color:#1a1a1a;background:#fafafa}"
            "pre{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;"
            "font-size:inherit;margin:0}a{color:#0066cc}</style>"
            f"</head><body><pre>{escaped}</pre></body></html>"
        )
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        key = f"deep_research/{user_id}/{timestamp}.html"
        return await media_storage.store(
            data=html_page.encode("utf-8"),
            key=key,
            content_type="text/html; charset=utf-8",
        )
    except Exception as exc:
        logger.error("[DeepResearch] Report upload failed: %s", exc, exc_info=True)
        return None


def _build_doc_planner_query(original_query: str, result_text: str) -> str:
    """Build the DocPlanner query from a completed research result."""
    parts = [
        "Create a professional research report document based on the following research findings.\n"
        "\n"
        "Apply document design best practices to ensure strong visual readability:\n"
        "- Clear hierarchy with a title, section headings, and subheadings\n"
        "- Tables for comparative data or multi-column information\n"
        "- Bullet lists for enumerated items; prose paragraphs for narrative content\n"
        "- An executive summary section at the top\n"
        "- Consistent spacing and formatting throughout\n"
    ]
    if original_query:
        parts.append(f"\nResearch topic: {original_query}\n")
    parts.append(f"\nResearch findings:\n{result_text}")
    return "".join(parts)


async def deliver_deep_research(
    result_text: str,
    user_id: str,
    account_id: str,
    query: str,
    task_queue: Optional[TaskQueue],
    session_id: str = "",
) -> None:
    """
    Deliver deep research result by creating a DOCX document via DocPlanner.

    Enqueues a create_document Cloud Task for DocPlannerAgent. DocPlanner builds
    a layout spec, delegates to DocGeneratorAgent, which produces DOCX bytes and
    delivers them via notify_file_bytes to the user's last active channel.
    """
    if not task_queue:
        logger.warning("[DeepResearch] No task_queue configured — DOCX delivery skipped")
        return

    doc_query = _build_doc_planner_query(query, result_text)

    try:
        await task_queue.enqueue_agent_task(
            agent_id="doc_planner_agent",
            intent="create_document",
            query=doc_query,
            context={
                "user_id": user_id,
                "account_id": account_id,
                "session_id": session_id or "",
            },
            deadline_seconds=720,
        )
        logger.info("[DeepResearch] DocPlanner task enqueued for user=%s", user_id[:8])
    except Exception as exc:
        logger.error(
            "[DeepResearch] Failed to enqueue DocPlanner task for user=%s: %s",
            user_id[:8], exc, exc_info=True,
        )
