"""
PuppeteerRunnerPort
===================

Port for rendering HTML to PDF via a Puppeteer subprocess.
System boundary: local Node.js subprocess (current) or Cloud Function (future).

Implementations:
  NodePuppeteerRunner — asyncio subprocess calling pdf_generator/runner.js.
  (future)            — Cloud Function HTTP call.
"""

from abc import ABC, abstractmethod


class PuppeteerRunnerError(Exception):
    """Raised by PuppeteerRunnerPort implementations on execution failure."""


class PuppeteerRunnerPort(ABC):

    @abstractmethod
    async def run(self, html_code: str, timeout: int) -> bytes:
        """
        Render an HTML document to PDF bytes via Puppeteer.

        Args:
            html_code: Complete HTML document (with embedded CSS).
                       Must be self-contained: no external stylesheets or fonts.
            timeout:   Execution timeout in seconds.

        Returns:
            Raw PDF bytes.

        Raises:
            PuppeteerRunnerError: on non-zero exit, empty stdout, or timeout.
        """
