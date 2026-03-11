"""Port contract tests for HtmlRendererPort."""
import pytest
from unittest.mock import AsyncMock

from src.ports.html_renderer_port import HtmlRendererPort, HtmlRenderError


class _ConcreteRenderer(HtmlRendererPort):
    """Minimal concrete implementation for contract verification."""

    async def render(self, html: str, width: int = 480) -> bytes:
        return b"\x89PNG"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def test_html_renderer_port_is_abstract():
    with pytest.raises(TypeError):
        HtmlRendererPort()  # type: ignore[abstract]


def test_concrete_renderer_satisfies_contract():
    renderer = _ConcreteRenderer()
    assert isinstance(renderer, HtmlRendererPort)


async def test_render_returns_bytes():
    renderer = _ConcreteRenderer()
    result = await renderer.render("<div>hello</div>")
    assert isinstance(result, bytes)


async def test_render_accepts_width_param():
    renderer = _ConcreteRenderer()
    result = await renderer.render("<div/>", width=320)
    assert isinstance(result, bytes)


def test_html_render_error_is_exception():
    err = HtmlRenderError("timeout")
    assert isinstance(err, Exception)
    assert str(err) == "timeout"
