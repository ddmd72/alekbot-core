"""
Deep Research Delivery Helpers
==============================

Shared by WorkerHandler, AgentWorkerHandler, and deep_research_webhooks.

- upload_html_report()    — wrap markdown in HTML, upload to GCS via MediaStoragePort
- deliver_deep_research() — two parallel notifications: SmartAgent summary + direct report link
- NotificationPort        — structural Protocol for UserNotificationService
"""

import asyncio
import html as html_lib
from datetime import datetime
from typing import Optional, Protocol

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


async def upload_html_report(
    markdown_text: str,
    user_id: str,
    media_storage: Optional[MediaStoragePort],
) -> Optional[str]:
    """Wrap markdown in HTML and upload to GCS. Returns public URL or None."""
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


async def deliver_deep_research(
    result_text: str,
    user_id: str,
    account_id: str,
    query: str,
    notification: NotificationPort,
    media_storage: Optional[MediaStoragePort],
    session_id: str = "",
) -> None:
    """
    Deliver deep research result via two parallel notifications:
    1. SmartAgent formats the report summary for the user.
    2. Direct link to the full HTML report (uploaded to GCS).
    """
    url = await upload_html_report(result_text, user_id, media_storage)

    # Build alert text for SmartAgent
    parts = [
        "[system_alert] Deep Research Agent has completed the user's research request.\n\n"
        "⚠️ CRITICAL DELIVERY INSTRUCTIONS — NO EXCEPTIONS:\n"
        "- You MUST deliver the COMPLETE research report below to the user, word for word.\n"
        "- Do NOT summarize, shorten, or paraphrase ANY part of the report.\n"
        "- Do NOT omit sections, findings, or details.\n"
        "- Your ONLY task is to reformat the text in the user's usual style (language, tone, markdown).\n"
        "- The full content of every section must appear in your response.\n\n",
    ]
    if query:
        parts.append(f"Original question:\n{query}\n\n")
    parts.append(f"Research report (deliver in full):\n{result_text}\n\n")
    if url:
        parts.append(f"Full report URL: {url}\n\n")
    parts.append(
        "Reminder: reformat for the user's preferred style, but preserve every word of the report above."
    )
    alert = "".join(parts)

    # Two parallel deliveries: SmartAgent summary + direct link
    coros = [
        notification.notify(
            user_id=user_id,
            account_id=account_id,
            system_alert=alert,
            agent_id_override=f"smart_response_agent_{user_id}",
            session_id=session_id or None,
        ),
    ]
    if url:
        coros.append(
            notification.notify_raw(
                user_id=user_id,
                account_id=account_id,
                text=f"📄 Full report: {url}",
            ),
        )
    await asyncio.gather(*coros)
