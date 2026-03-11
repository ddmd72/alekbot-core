"""
HtmlRendererPort — abstract interface for rendering HTML to PNG bytes.

Implementations:
  PlaywrightHtmlRenderer — headless Chromium via Playwright
  MockHtmlRenderer       — returns fixed bytes for tests
"""
from abc import ABC, abstractmethod


class HtmlRenderError(Exception):
    """Raised when HTML rendering fails (timeout, crash, invalid content)."""


class HtmlRendererPort(ABC):
    """Renders an HTML string to PNG bytes."""

    @abstractmethod
    async def render(self, html: str, width: int = 480) -> bytes:
        """
        Render HTML to PNG.

        Args:
            html:  Complete, self-contained HTML (inline CSS, no external deps, no JS).
            width: Viewport width in pixels. Height auto-fits content.

        Returns:
            PNG bytes.

        Raises:
            HtmlRenderError: On timeout or render failure.
        """
        ...

    @abstractmethod
    async def start(self) -> None:
        """Launch the underlying browser/renderer. Called once at application startup."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shut down the renderer. Called on graceful shutdown."""
        ...
