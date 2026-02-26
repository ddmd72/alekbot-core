"""
Unit tests for RichContentService — html_card type.

MockHtmlRenderer returns fixed PNG bytes so tests have no Playwright dependency.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.rich_content_service import RichContentService
from src.ports.platform_media_port import PlatformMediaPort
from src.ports.html_renderer_port import HtmlRendererPort, HtmlRenderError
from src.domain.messaging import RichContent

_PNG = b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


def _make_service(renderer=None):
    media_port = AsyncMock(spec=PlatformMediaPort)
    return RichContentService(
        media_port=media_port,
        html_renderer=renderer,
    ), media_port


def _html_card(html="<div>test</div>", alt_text="Test card"):
    return RichContent(
        content_type="html_card",
        data={"html": html, "alt_text": alt_text},
        fallback_text="fallback",
    )


# ---------------------------------------------------------------------------
# Normal render path
# ---------------------------------------------------------------------------

async def test_html_card_renders_and_uploads():
    renderer = AsyncMock(spec=HtmlRendererPort)
    renderer.render.return_value = _PNG
    service, media_port = _make_service(renderer)

    result = await service.process(_html_card(), channel_id="C123")

    renderer.render.assert_awaited_once()
    html_arg = renderer.render.call_args.args[0]
    assert "<div>test</div>" in html_arg

    media_port.upload_image.assert_awaited_once()
    call = media_port.upload_image.call_args
    assert call.kwargs["image_bytes"] == _PNG
    assert call.kwargs["alt_text"] == "Test card"
    assert call.kwargs["channel_id"] == "C123"

    assert result is None  # html_card never returns a URL


# ---------------------------------------------------------------------------
# Renderer not configured
# ---------------------------------------------------------------------------

async def test_html_card_skipped_when_no_renderer():
    service, media_port = _make_service(renderer=None)

    result = await service.process(_html_card(), channel_id="C123")

    media_port.upload_image.assert_not_awaited()
    assert result is None


# ---------------------------------------------------------------------------
# Empty html field
# ---------------------------------------------------------------------------

async def test_html_card_skipped_when_html_empty():
    renderer = AsyncMock(spec=HtmlRendererPort)
    service, media_port = _make_service(renderer)

    empty_card = RichContent(
        content_type="html_card",
        data={"html": "", "alt_text": "Empty"},
        fallback_text="",
    )
    await service.process(empty_card, channel_id="C123")

    renderer.render.assert_not_awaited()
    media_port.upload_image.assert_not_awaited()


# ---------------------------------------------------------------------------
# HtmlRenderError — graceful, no exception propagated
# ---------------------------------------------------------------------------

async def test_html_card_render_error_does_not_propagate():
    renderer = AsyncMock(spec=HtmlRendererPort)
    renderer.render.side_effect = HtmlRenderError("timeout")
    service, media_port = _make_service(renderer)

    # Must not raise
    result = await service.process(_html_card(), channel_id="C123")

    media_port.upload_image.assert_not_awaited()
    assert result is None


# ---------------------------------------------------------------------------
# Default alt_text when missing
# ---------------------------------------------------------------------------

async def test_html_card_default_alt_text():
    renderer = AsyncMock(spec=HtmlRendererPort)
    renderer.render.return_value = _PNG
    service, media_port = _make_service(renderer)

    card = RichContent(
        content_type="html_card",
        data={"html": "<p>hi</p>"},  # no alt_text
        fallback_text="",
    )
    await service.process(card, channel_id="C123")

    call = media_port.upload_image.call_args
    assert call.kwargs["alt_text"] == "Visual card"
