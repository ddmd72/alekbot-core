"""
Unit tests for RichContentService.

Coverage:
  process()
    - content_type="file" → delegates to _handle_file
    - content_type="widget" → delegates to _handle_widget, returns None
    - unsupported content_type → logs warning, returns None

  _handle_file()
    - missing 'content' field → returns None
    - .html + storage_port → returns GCS URL
    - .html without storage_port → falls through to utf-8 upload, returns None
    - .md / .txt → utf-8 encoded, uploaded as file, returns None
    - .xlsx → csv_to_xlsx conversion, uploaded
    - .docx → markdown_to_docx conversion, uploaded
    - conversion error → falls back to plain text upload
    - filename without extension → defaults to .md (utf-8 encode)

  _handle_widget()
    - no html_renderer → logs warning, no upload
    - missing 'html' field → skips
    - successful render → uploads PNG
    - HtmlRenderError → logs error, no re-raise
    - generic upload error → logs error, no re-raise

  upload_file_bytes()
    - calls media_port.upload_file with given args

  _store_html()
    - stores bytes + returns URL
    - GCS exception → returns None
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.messaging import RichContent
from src.ports.html_renderer_port import HtmlRenderError
from src.services.rich_content_service import RichContentService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CHANNEL = "C-test"


def _make_service(*, with_storage=True, with_renderer=True):
    media_port = MagicMock()
    media_port.upload_file = AsyncMock()
    media_port.upload_image = AsyncMock()

    storage_port = MagicMock() if with_storage else None
    if storage_port:
        storage_port.store = AsyncMock(return_value="https://gcs.example.com/file.html")

    html_renderer = MagicMock() if with_renderer else None
    if html_renderer:
        html_renderer.render = AsyncMock(return_value=b"\x89PNG\r\n")

    svc = RichContentService(
        media_port=media_port,
        storage_port=storage_port,
        html_renderer=html_renderer,
    )
    return svc, media_port, storage_port, html_renderer


def _rich(content_type, data):
    return RichContent(content_type=content_type, data=data, fallback_text="fallback")


# ---------------------------------------------------------------------------
# process()
# ---------------------------------------------------------------------------

class TestProcess:

    async def test_file_type_returns_handle_file_result(self):
        svc, _, storage, _ = _make_service()
        content = _rich("file", {"filename": "page.html", "content": "<html/>"})
        result = await svc.process(content, _CHANNEL)
        assert result == "https://gcs.example.com/file.html"

    async def test_widget_type_returns_none(self):
        svc, media, _, renderer = _make_service()
        content = _rich("widget", {"html": "<div>card</div>", "alt_text": "card"})
        result = await svc.process(content, _CHANNEL)
        assert result is None

    async def test_unsupported_type_returns_none(self):
        svc, media, _, _ = _make_service()
        content = _rich("unknown_type", {})
        result = await svc.process(content, _CHANNEL)
        assert result is None
        media.upload_file.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_file()
# ---------------------------------------------------------------------------

class TestHandleFile:

    async def test_missing_content_returns_none(self):
        svc, media, _, _ = _make_service()
        content = _rich("file", {"filename": "doc.md"})
        result = await svc._handle_file(content, _CHANNEL)
        assert result is None
        media.upload_file.assert_not_called()

    async def test_html_with_storage_returns_url(self):
        svc, _, storage, _ = _make_service()
        content = _rich("file", {"filename": "page.html", "content": "<html>hi</html>"})
        result = await svc._handle_file(content, _CHANNEL)
        assert result == "https://gcs.example.com/file.html"
        storage.store.assert_called_once()

    async def test_html_without_storage_uploads_as_file(self):
        svc, media, _, _ = _make_service(with_storage=False)
        content = _rich("file", {"filename": "page.html", "content": "<html>hi</html>"})
        result = await svc._handle_file(content, _CHANNEL)
        assert result is None
        media.upload_file.assert_called_once()

    async def test_md_file_utf8_encoded_and_uploaded(self):
        svc, media, _, _ = _make_service()
        content = _rich("file", {"filename": "notes.md", "content": "# Hello"})
        result = await svc._handle_file(content, _CHANNEL)
        assert result is None
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["file_bytes"] == "# Hello".encode("utf-8")
        assert call_kwargs["filename"] == "notes.md"

    async def test_txt_file_utf8_encoded(self):
        svc, media, _, _ = _make_service()
        content = _rich("file", {"filename": "out.txt", "content": "plain text"})
        await svc._handle_file(content, _CHANNEL)
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["file_bytes"] == b"plain text"

    async def test_xlsx_calls_csv_to_xlsx(self):
        svc, media, _, _ = _make_service()
        csv_data = "a,b,c\n1,2,3\n"
        content = _rich("file", {"filename": "data.xlsx", "content": csv_data})
        with patch("src.services.rich_content_service._csv_to_xlsx", return_value=b"xlsx-bytes") as mock_conv:
            await svc._handle_file(content, _CHANNEL)
        mock_conv.assert_called_once_with(csv_data)
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["file_bytes"] == b"xlsx-bytes"

    async def test_docx_calls_markdown_to_docx(self):
        svc, media, _, _ = _make_service()
        md = "# Doc\n\nParagraph."
        content = _rich("file", {"filename": "report.docx", "content": md})
        with patch("src.services.rich_content_service._markdown_to_docx", return_value=b"docx-bytes") as mock_conv:
            await svc._handle_file(content, _CHANNEL)
        mock_conv.assert_called_once_with(md)
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["file_bytes"] == b"docx-bytes"

    async def test_conversion_error_falls_back_to_txt(self):
        svc, media, _, _ = _make_service()
        content = _rich("file", {"filename": "data.xlsx", "content": "bad,csv"})
        with patch("src.services.rich_content_service._csv_to_xlsx", side_effect=Exception("openpyxl missing")):
            await svc._handle_file(content, _CHANNEL)
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["filename"] == "data.txt"
        assert call_kwargs["file_bytes"] == b"bad,csv"

    async def test_filename_without_extension_defaults_to_utf8(self):
        svc, media, _, _ = _make_service()
        content = _rich("file", {"filename": "noext", "content": "some text"})
        await svc._handle_file(content, _CHANNEL)
        call_kwargs = media.upload_file.call_args.kwargs
        assert call_kwargs["file_bytes"] == b"some text"


# ---------------------------------------------------------------------------
# _handle_widget()
# ---------------------------------------------------------------------------

class TestHandleWidget:

    async def test_no_html_renderer_skips_upload(self):
        svc, media, _, _ = _make_service(with_renderer=False)
        content = _rich("widget", {"html": "<div/>", "alt_text": "card"})
        await svc._handle_widget(content, _CHANNEL)
        media.upload_image.assert_not_called()

    async def test_missing_html_field_skips(self):
        svc, media, _, _ = _make_service()
        content = _rich("widget", {"alt_text": "card"})
        await svc._handle_widget(content, _CHANNEL)
        media.upload_image.assert_not_called()

    async def test_successful_render_uploads_png(self):
        svc, media, _, renderer = _make_service()
        content = _rich("widget", {"html": "<div>card</div>", "alt_text": "My Card"})
        await svc._handle_widget(content, _CHANNEL)
        renderer.render.assert_called_once_with("<div>card</div>")
        media.upload_image.assert_called_once()
        call_kwargs = media.upload_image.call_args.kwargs
        assert call_kwargs["alt_text"] == "My Card"

    async def test_html_render_error_no_reraise(self):
        svc, media, _, renderer = _make_service()
        renderer.render = AsyncMock(side_effect=HtmlRenderError("timeout"))
        content = _rich("widget", {"html": "<div/>", "alt_text": "card"})
        await svc._handle_widget(content, _CHANNEL)  # must not raise
        media.upload_image.assert_not_called()

    async def test_upload_error_no_reraise(self):
        svc, media, _, renderer = _make_service()
        renderer.render = AsyncMock(return_value=b"\x89PNG")
        media.upload_image = AsyncMock(side_effect=RuntimeError("network"))
        content = _rich("widget", {"html": "<div/>", "alt_text": "card"})
        await svc._handle_widget(content, _CHANNEL)  # must not raise


# ---------------------------------------------------------------------------
# upload_file_bytes()
# ---------------------------------------------------------------------------

class TestUploadFileBytes:

    async def test_delegates_to_media_port(self):
        svc, media, _, _ = _make_service()
        await svc.upload_file_bytes(
            file_bytes=b"docx",
            filename="out.docx",
            title="My Doc",
            channel_id=_CHANNEL,
        )
        media.upload_file.assert_called_once_with(
            file_bytes=b"docx",
            filename="out.docx",
            title="My Doc",
            channel_id=_CHANNEL,
        )


# ---------------------------------------------------------------------------
# _store_html()
# ---------------------------------------------------------------------------

class TestStoreHtml:

    async def test_stores_and_returns_url(self):
        svc, _, storage, _ = _make_service()
        url = await svc._store_html("<html>test</html>", "page.html")
        assert url == "https://gcs.example.com/file.html"
        storage.store.assert_called_once()
        call_kwargs = storage.store.call_args.kwargs
        assert call_kwargs["content_type"] == "text/html; charset=utf-8"
        assert call_kwargs["data"] == "<html>test</html>".encode("utf-8")

    async def test_gcs_exception_returns_none(self):
        svc, _, storage, _ = _make_service()
        storage.store = AsyncMock(side_effect=Exception("GCS down"))
        url = await svc._store_html("<html/>", "page.html")
        assert url is None


# ==============================================================================
# _csv_to_xlsx (lines 189-201)
# ==============================================================================

class TestCsvToXlsx:

    def test_basic_csv_produces_xlsx_bytes(self):
        from src.services.rich_content_service import _csv_to_xlsx
        result = _csv_to_xlsx("name,age\nAlice,30\nBob,25")
        # xlsx starts with PK (zip magic bytes)
        assert result[:2] == b"PK"
        assert len(result) > 0

    def test_single_row(self):
        from src.services.rich_content_service import _csv_to_xlsx
        result = _csv_to_xlsx("header1,header2")
        assert result[:2] == b"PK"

    def test_empty_csv(self):
        from src.services.rich_content_service import _csv_to_xlsx
        result = _csv_to_xlsx("")
        assert isinstance(result, bytes)


# ==============================================================================
# _markdown_to_docx (lines 204-233)
# ==============================================================================

class TestMarkdownToDocx:

    def test_basic_paragraph(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("Hello world")
        assert result[:2] == b"PK"

    def test_h1_heading(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("# Title")
        assert isinstance(result, bytes) and len(result) > 0

    def test_h2_heading(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("## Section")
        assert isinstance(result, bytes) and len(result) > 0

    def test_h3_heading(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("### Subsection")
        assert isinstance(result, bytes) and len(result) > 0

    def test_bullet_list(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("- item one\n* item two")
        assert isinstance(result, bytes) and len(result) > 0

    def test_numbered_list(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("1. first\n2. second")
        assert isinstance(result, bytes) and len(result) > 0

    def test_horizontal_rule(self):
        from src.services.rich_content_service import _markdown_to_docx
        result = _markdown_to_docx("---")
        assert isinstance(result, bytes) and len(result) > 0

    def test_mixed_content(self):
        from src.services.rich_content_service import _markdown_to_docx
        md = "# Title\n\nSome text.\n\n- bullet\n\n1. numbered\n\n---\n\n## End"
        result = _markdown_to_docx(md)
        assert result[:2] == b"PK"


# ==============================================================================
# _apply_inline (lines 236-249)
# ==============================================================================

class TestApplyInline:

    def _make_paragraph(self):
        from docx import Document
        doc = Document()
        return doc.add_paragraph()

    def test_plain_text(self):
        from src.services.rich_content_service import _apply_inline
        p = self._make_paragraph()
        _apply_inline(p, "plain text")
        assert p.runs[0].text == "plain text"
        assert not p.runs[0].bold
        assert not p.runs[0].italic

    def test_bold_text(self):
        from src.services.rich_content_service import _apply_inline
        p = self._make_paragraph()
        _apply_inline(p, "**bold**")
        bold_run = next(r for r in p.runs if r.bold)
        assert bold_run.text == "bold"

    def test_italic_text(self):
        from src.services.rich_content_service import _apply_inline
        p = self._make_paragraph()
        _apply_inline(p, "*italic*")
        italic_run = next(r for r in p.runs if r.italic)
        assert italic_run.text == "italic"

    def test_mixed_inline(self):
        from src.services.rich_content_service import _apply_inline
        p = self._make_paragraph()
        _apply_inline(p, "start **bold** middle *italic* end")
        texts = [r.text for r in p.runs]
        assert "bold" in texts
        assert "italic" in texts
        assert any("start" in t for t in texts)
