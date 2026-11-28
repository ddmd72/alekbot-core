"""
RichContentService — application-layer dispatcher for rich content delivery.

Agents declare *what* to generate (RichContent); this service handles *how*:
  - converts LLM-generated text to binary formats (xlsx, docx)
  - .html files → private media bucket + a `/f/<token>` capability link (returned to the caller for link
    delivery; needs the user id and the link service), else uploaded to the platform like any other file
  - other files → direct platform upload via PlatformMediaPort

ConversationHandler calls process() for each non-table rich content item
after the text response has been delivered.

Returns:
  Optional[str] — the `/f/<token>` link when an html page was stored privately,
                  None when content was uploaded as a file attachment.

Supported types:
  file      — LLM-generated content
              .html              → private upload → return the `/f/<token>` link (no user id → platform upload)
              .md / .txt         → UTF-8 encode → platform upload
              .xlsx (CSV→xlsx)   → platform upload
              .docx (Markdown→docx) → platform upload
  widget    — Agent-generated HTML → Playwright screenshot → PNG → platform upload (inline image)

Planned (M3+):
  map_image — Google Maps Static API → GCS upload → return URL
"""
import csv
import io
import uuid
from typing import TYPE_CHECKING, Optional

from ..ports.platform_media_port import PlatformMediaPort
from ..ports.media_storage_port import MediaStoragePort
from ..ports.html_renderer_port import HtmlRendererPort, HtmlRenderError
from ..domain.messaging import MEDIA_CONTENT_TYPES as _MEDIA_CONTENT_TYPES, ResponseChannel, RichContent
from ..utils.logger import logger

if TYPE_CHECKING:
    # Type-only import (REQ-ARCH-22: services must not import services at runtime); injected via the constructor.
    from .file_link_service import FileLinkService


# storage_class → object-key prefix of a stored html page. A page without a class keeps the default `html/` prefix;
# `email_review/` is the prefix FileLinkService gates behind the Cabinet session with the short link lifetime.
_STORAGE_CLASS_PREFIX = {"email_review": "email_review"}


class RichContentService:
    """Converts and delivers rich content items via PlatformMediaPort / MediaStoragePort."""

    def __init__(
        self,
        media_port: PlatformMediaPort,
        storage_port: Optional[MediaStoragePort] = None,
        html_renderer: Optional[HtmlRendererPort] = None,
        link_service: Optional["FileLinkService"] = None,
    ) -> None:
        self._media_port = media_port
        self._storage_port = storage_port
        self._html_renderer = html_renderer
        # The bucket is private: a stored page reaches the user only through a `/f/<token>` capability link
        # (decisions/rich_content_one_delivery_path.md), never as its storage key.
        self._link_service = link_service

    async def process(
        self,
        content: RichContent,
        channel_id: str,
        user_id: Optional[str] = None,
        storage_class: Optional[str] = None,
    ) -> Optional[str]:
        """
        Process a single RichContent item and deliver it to the platform.

        Args:
            content:    Structured content descriptor from LLM output
            channel_id: Platform-specific channel identifier
            user_id:    The owner, for an html page's capability link; without it the page is uploaded as a file
            storage_class: The run's storage class (e.g. "email_review"); an html page is stored under its key prefix

        Returns:
            The `/f/<token>` link if an html page was stored privately,
            None for all other types (direct upload or skip).
        """
        if content.content_type == "file":
            return await self._handle_file(content, channel_id, user_id, storage_class)
        elif content.content_type == "widget":
            await self._handle_widget(content, channel_id)
            return None
        else:
            logger.warning(
                "RichContentService: unsupported content type '%s' — skipping",
                content.content_type,
            )
            return None

    async def deliver(
        self,
        content: RichContent,
        response_channel: ResponseChannel,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        storage_class: Optional[str] = None,
    ) -> None:
        """
        Route one rich-content item to the right delivery path (chat turns and background notifications alike):
          - media types (file, widget, ...) → process() on the channel's id (convert + upload, or GCS URL)
          - table and others → ResponseChannel.send_rich_content (Block Kit / fallback)

        When process() returns a link (an html page → `/f/<token>`), it is sent as a plain message so
        Slack unfurls it as a preview. `user_id` is the owner the link is minted for; `storage_class` files the page
        under that class's key prefix.
        """
        if content.content_type not in _MEDIA_CONTENT_TYPES:
            await response_channel.send_rich_content(content, thread_id=thread_id)
            return
        channel_id = getattr(response_channel, "channel_id", None)
        if not channel_id:
            logger.warning(
                "RichContentService: response_channel has no channel_id — "
                "falling back to send_rich_content for type '%s'",
                content.content_type,
            )
            await response_channel.send_rich_content(content, thread_id=thread_id)
            return
        url = await self.process(content, channel_id, user_id, storage_class)
        if url:
            await response_channel.send_message(url, thread_id=thread_id)

    # ------------------------------------------------------------------
    # Private handlers
    # ------------------------------------------------------------------

    async def _handle_file(
        self, content: RichContent, channel_id: str, user_id: Optional[str] = None, storage_class: Optional[str] = None,
    ) -> Optional[str]:
        filename = content.data.get("filename", "document.md").strip()
        title = content.data.get("title", filename).strip()
        text_content = content.data.get("content", "")

        if not text_content:
            logger.warning("RichContentService: file type missing 'content' in data")
            return None

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "md"

        # HTML → private bucket + capability link, when one can be minted for this user; otherwise, or when the
        # private store fails (logged as ERROR there), the page is uploaded below like any other file (as on
        # Telegram), never posted as its storage key and never dropped.
        if ext == "html" and self._can_link(user_id):
            link = await self._store_html(text_content, filename, user_id, storage_class)
            if link:
                return link

        # All other formats → encode/convert → platform file upload
        try:
            if ext == "xlsx":
                file_bytes = _csv_to_xlsx(text_content)
            elif ext == "docx":
                file_bytes = _markdown_to_docx(text_content)
            else:
                file_bytes = text_content.encode("utf-8")
        except Exception as e:
            logger.error(
                "RichContentService: conversion failed for '%s' — %s; falling back to plain text",
                filename,
                e,
            )
            file_bytes = text_content.encode("utf-8")
            filename = filename.rsplit(".", 1)[0] + ".txt"

        await self._media_port.upload_file(
            file_bytes=file_bytes,
            filename=filename,
            title=title,
            channel_id=channel_id,
        )
        return None

    async def _handle_widget(self, content: RichContent, channel_id: str) -> None:
        """Render HTML to PNG via HtmlRendererPort and upload as inline image."""
        if not self._html_renderer:
            logger.warning("RichContentService: widget received but HtmlRendererPort not configured")
            return

        html = content.data.get("html", "").strip()
        alt_text = content.data.get("alt_text", "Visual card")

        if not html:
            logger.warning("RichContentService: widget missing 'html' field — skipping")
            return

        try:
            png_bytes = await self._html_renderer.render(html)
            await self._media_port.upload_image(
                image_bytes=png_bytes,
                alt_text=alt_text,
                channel_id=channel_id,
            )
        except HtmlRenderError as e:
            logger.error("RichContentService: widget render failed — %s", e)
        except Exception as e:
            logger.error("RichContentService: widget upload failed — %s", e)

    async def upload_file_bytes(
        self,
        file_bytes: bytes,
        filename: str,
        title: str,
        channel_id: str,
    ) -> None:
        """
        Direct upload of pre-built file bytes to the platform.

        Bypasses all text conversion logic — used by DocPlannerAgent to deliver
        DOCX bytes generated by DocGeneratorAgent.
        """
        await self._media_port.upload_file(
            file_bytes=file_bytes,
            filename=filename,
            title=title,
            channel_id=channel_id,
        )

    def _can_link(self, user_id: Optional[str]) -> bool:
        return bool(self._storage_port and self._link_service and user_id)

    async def _store_html(
        self, html_content: str, filename: str, user_id: Optional[str] = None, storage_class: Optional[str] = None,
    ) -> Optional[str]:
        """Store the page in the private bucket and return its `/f/<token>` link for `user_id`; None (nothing
        stored) when no link can be minted or the upload fails."""
        storage, links = self._storage_port, self._link_service
        if storage is None or links is None or not user_id:
            logger.warning("RichContentService: no capability link for '%s' (no storage, link service or user id)",
                           filename)
            return None
        try:
            html_bytes = html_content.encode("utf-8")
            prefix = _STORAGE_CLASS_PREFIX.get(storage_class or "")
            key = f"{prefix}/{user_id}/{uuid.uuid4()}-{filename}" if prefix else f"html/{uuid.uuid4()}-{filename}"
            stored_key = await storage.store(
                data=html_bytes,
                key=key,
                content_type="text/html; charset=utf-8",
            )
            return links.build_link(key=stored_key, user_id=user_id)
        except Exception as e:
            logger.error(
                "RichContentService: GCS upload failed for '%s' — %s; no link",
                filename,
                e,
            )
            return None


# ------------------------------------------------------------------
# Format converters
# ------------------------------------------------------------------

def _csv_to_xlsx(csv_content: str) -> bytes:
    """Convert CSV string to xlsx bytes via openpyxl."""
    from openpyxl import Workbook  # lazy import — optional dependency

    reader = csv.reader(io.StringIO(csv_content))
    wb = Workbook()
    ws = wb.active
    for row in reader:
        ws.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _markdown_to_docx(md_content: str) -> bytes:
    """Convert Markdown string to docx bytes via python-docx."""
    import re
    from docx import Document  # lazy import — optional dependency

    doc = Document()
    lines = md_content.splitlines()

    for line in lines:
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:].strip(), level=1)
        elif line.startswith("- ") or line.startswith("* "):
            p = doc.add_paragraph(style="List Bullet")
            _apply_inline(p, line[2:].strip())
        elif re.match(r"^\d+\. ", line):
            p = doc.add_paragraph(style="List Number")
            _apply_inline(p, re.sub(r"^\d+\. ", "", line).strip())
        elif line.strip() == "---":
            doc.add_paragraph("─" * 40)
        elif line.strip():
            p = doc.add_paragraph()
            _apply_inline(p, line.strip())

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _apply_inline(paragraph, text: str) -> None:
    """Apply bold/italic inline markdown to a docx paragraph run."""
    import re

    parts = re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*"):
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        else:
            paragraph.add_run(part)
