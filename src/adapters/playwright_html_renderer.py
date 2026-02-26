"""
PlaywrightHtmlRenderer — HtmlRendererPort implementation via headless Chromium.

Browser lifecycle:
  - Lazy init: browser starts on first render() call, not at container startup.
  - Singleton: one Chromium instance per process, reused across requests.
  - asyncio.Lock guards concurrent first-call initialization.
  - is_connected() check before each render detects browser crashes and reconnects.

Cloud Run:
  - Chromium requires --no-sandbox in non-root containers.
  - Detected automatically via K_SERVICE env var (set by Cloud Run runtime).

Local:
  - Runs in normal sandboxed mode — no extra flags needed.
"""
import asyncio
import os
from typing import Optional

from ..ports.html_renderer_port import HtmlRendererPort, HtmlRenderError
from ..utils.logger import logger

_RENDER_TIMEOUT_MS = 8_000
_DEVICE_SCALE_FACTOR = 2  # Retina-quality output


class PlaywrightHtmlRenderer(HtmlRendererPort):
    """Renders HTML to PNG via a shared headless Chromium browser."""

    def __init__(self) -> None:
        self._browser = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._is_cloud_run = bool(os.getenv("K_SERVICE"))

    async def start(self) -> None:
        """No-op — browser starts lazily on first render() call."""

    async def stop(self) -> None:
        """Close browser on graceful shutdown."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning("PlaywrightHtmlRenderer: error closing browser — %s", e)
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning("PlaywrightHtmlRenderer: error stopping playwright — %s", e)
            self._playwright = None

    async def render(self, html: str, width: int = 480) -> bytes:
        """Render HTML to PNG bytes. Lazy-starts browser on first call."""
        await self._ensure_browser()

        page = None
        try:
            page = await self._browser.new_page(
                viewport={"width": width, "height": 800},
                device_scale_factor=_DEVICE_SCALE_FACTOR,
            )
            await page.set_content(html, wait_until="networkidle", timeout=_RENDER_TIMEOUT_MS)
            # Reset browser default margins. Keep body height as fit-content so it wraps the
            # widget without stretching to viewport height. Do NOT override background — the
            # LLM may place the widget background on <body> itself (full-page layout).
            await page.add_style_tag(content="html,body{margin:0;padding:0;height:fit-content!important}")
            # Detect widget structure:
            #   Fragment (LLM returns bare <div>): body has 1 child → screenshot that child.
            #   Full page (LLM uses <body> as widget root with 2+ children): screenshot body.
            child_count = await page.evaluate("document.body.children.length")
            if child_count == 1:
                element = await page.query_selector("body > *:first-child")
            else:
                element = await page.query_selector("body")
            if element is None:
                element = await page.query_selector("body")
            png = await element.screenshot(omit_background=True)
            logger.debug("PlaywrightHtmlRenderer: rendered %d bytes PNG", len(png))
            return png
        except Exception as e:
            raise HtmlRenderError(f"render failed: {e}") from e
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    async def _ensure_browser(self) -> None:
        """Start browser if not running, or reconnect if it crashed."""
        if self._browser and self._browser.is_connected():
            return
        async with self._lock:
            # Re-check under lock — another coroutine may have started it already.
            if self._browser and self._browser.is_connected():
                return
            await self._launch_browser()

    async def _launch_browser(self) -> None:
        from playwright.async_api import async_playwright  # lazy import

        logger.info("PlaywrightHtmlRenderer: launching Chromium (cloud_run=%s)", self._is_cloud_run)
        self._playwright = await async_playwright().start()

        launch_args = []
        if self._is_cloud_run:
            # Required in Cloud Run non-root containers.
            launch_args = ["--no-sandbox", "--disable-setuid-sandbox"]

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
        )
        logger.info("PlaywrightHtmlRenderer: Chromium ready")
