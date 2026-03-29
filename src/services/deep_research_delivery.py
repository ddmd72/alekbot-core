"""
Deep Research Delivery Helpers
==============================

Shared by WorkerHandler, AgentWorkerHandler, and deep_research_webhooks.

- upload_html_report()    — wrap markdown in HTML, upload to GCS via MediaStoragePort
                            (kept for debugging; not called from deliver_deep_research)
- _upload_round()         — upload raw markdown round text to GCS as .md file
- deliver_deep_research() — upload round files, send named links, enqueue HtmlPageGenerator task
- NotificationPort        — structural Protocol for UserNotificationService
"""

import html as html_lib
from datetime import datetime
from typing import Any, Optional, Protocol

from ..ports.media_storage_port import MediaStoragePort
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


async def _upload_round(
    text: str,
    user_id: str,
    timestamp: str,
    suffix: str,
    media_storage: MediaStoragePort,
) -> Optional[str]:
    """Upload a raw markdown research round to GCS. Returns public URL or None.

    Args:
        text:          Raw markdown text from the research loop.
        user_id:       Used as a path segment for namespacing.
        timestamp:     UTC timestamp string (e.g. "20260316T123456Z").
        suffix:        File suffix, e.g. "round1", "round2", "report".
        media_storage: GCS storage port.
    """
    try:
        key = f"deep_research/{user_id}/{timestamp}-{suffix}.md"
        url = await media_storage.store(
            data=text.encode("utf-8"),
            key=key,
            content_type="text/markdown; charset=utf-8",
        )
        logger.info("[DeepResearch] Uploaded %s (%d chars) → %s", suffix, len(text), url)
        return url
    except Exception as exc:
        logger.error("[DeepResearch] Round upload failed (suffix=%s): %s", suffix, exc, exc_info=True)
        return None


def _build_html_page_query(original_query: str, result_text: str) -> str:
    """Build the HtmlPageGenerator query from a completed research result."""
    parts = [
        "Create an HTML page for the following research. Every word is substantive — include all of it.\n"
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
    task_queue: Optional[Any],  # TaskQueue or TaskDispatchService — duck-typed
    session_id: str = "",
    round1_text: str = "",
    media_storage: Optional[MediaStoragePort] = None,
    notification: Optional["NotificationPort"] = None,
) -> None:
    """
    Deliver deep research result:
      1. Upload round markdown files to GCS and send named links to the user.
      2. Enqueue HtmlPageGenerator Cloud Task → styled HTML report delivered to user.

    round1_text — raw first-pass result before the critic second pass.
                  If equal to result_text (second pass disabled), uploaded once as "report".
    """
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    has_two_rounds = bool(round1_text) and round1_text != result_text

    if media_storage and notification:
        if has_two_rounds:
            url1 = await _upload_round(round1_text, user_id, timestamp, "round1", media_storage)
            if url1:
                try:
                    await notification.notify_document_link(
                        user_id=user_id, account_id=account_id,
                        url=url1, label="Round 1 — raw research",
                    )
                except Exception as exc:
                    logger.error("[DeepResearch] notify_document_link round1 failed: %s", exc, exc_info=True)

            url2 = await _upload_round(result_text, user_id, timestamp, "round2", media_storage)
            if url2:
                try:
                    await notification.notify_document_link(
                        user_id=user_id, account_id=account_id,
                        url=url2, label="Round 2 — verified report",
                    )
                except Exception as exc:
                    logger.error("[DeepResearch] notify_document_link round2 failed: %s", exc, exc_info=True)
        else:
            url = await _upload_round(result_text, user_id, timestamp, "report", media_storage)
            if url:
                try:
                    await notification.notify_document_link(
                        user_id=user_id, account_id=account_id,
                        url=url, label="Research report (raw)",
                    )
                except Exception as exc:
                    logger.error("[DeepResearch] notify_document_link report failed: %s", exc, exc_info=True)
    else:
        logger.warning(
            "[DeepResearch] media_storage or notification not configured — skipping round uploads"
        )

    if not task_queue:
        logger.warning("[DeepResearch] No task_queue configured — HTML page delivery skipped")
        return

    html_query = _build_html_page_query(query, result_text)

    try:
        await task_queue.enqueue_agent_task(
            agent_id="html_page_generator_agent",
            intent="create_html_page",
            query=html_query,
            context={
                "user_id": user_id,
                "account_id": account_id,
                "session_id": session_id or "",
            },
            deadline_seconds=720,
        )
        logger.info("[DeepResearch] HtmlPageGenerator task enqueued for user=%s", user_id[:8])
    except Exception as exc:
        logger.error(
            "[DeepResearch] Failed to enqueue HtmlPageGenerator task for user=%s: %s",
            user_id[:8], exc, exc_info=True,
        )
