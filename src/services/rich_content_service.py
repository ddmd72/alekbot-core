"""
RichContentService — application-layer dispatcher for rich content delivery.

Agents declare *what* to generate (RichContent); this service handles *how*:
  - converts LLM-generated text to binary formats (xlsx, docx)
  - delegates binary upload to PlatformMediaPort (platform-agnostic)

ConversationHandler calls process() for each non-table rich content item
after the text response has been delivered.

Supported types:
  file  — LLM-generated content → platform upload (M2)
          Formats: .md, .html (raw UTF-8), .xlsx (CSV→xlsx), .docx (Markdown→docx)

Planned (M3+):
  map_image — Google Maps Static API → platform upload
"""
import csv
import io

from ..ports.platform_media_port import PlatformMediaPort
from ..domain.messaging import RichContent
from ..utils.logger import logger


class RichContentService:
    """Converts and delivers rich content items via PlatformMediaPort."""

    def __init__(self, media_port: PlatformMediaPort) -> None:
        self._media_port = media_port

    async def process(self, content: RichContent, channel_id: str) -> None:
        """
        Process a single RichContent item and deliver it to the platform.

        Args:
            content:    Structured content descriptor from LLM output
            channel_id: Platform-specific channel identifier
        """
        if content.content_type == "file":
            await self._handle_file(content, channel_id)
        else:
            logger.warning(
                "RichContentService: unsupported content type '%s' — skipping",
                content.content_type,
            )

    # ------------------------------------------------------------------
    # Private handlers
    # ------------------------------------------------------------------

    async def _handle_file(self, content: RichContent, channel_id: str) -> None:
        filename = content.data.get("filename", "document.md").strip()
        title = content.data.get("title", filename).strip()
        text_content = content.data.get("content", "")

        if not text_content:
            logger.warning("RichContentService: file type missing 'content' in data")
            return

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "md"

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
