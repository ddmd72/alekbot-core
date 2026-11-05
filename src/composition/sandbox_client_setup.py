"""
Main-service side of the untrusted-content sandbox (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.9, §4.12).

Public interface:
    build_docx_runner(getenv=os.getenv) -> DocxRunnerPort   (one runner)
    docx_runner_factory(getenv=os.getenv) -> Callable[[], DocxRunnerPort]
    build_pdf_runner(getenv=os.getenv) -> PuppeteerRunnerPort
    pdf_runner_factory(getenv=os.getenv) -> Callable[[], PuppeteerRunnerPort]
    build_html_renderer(enabled, inprocess_factory) -> Optional[HtmlRendererPort]

DOCX and PDF render only through the render sandbox; there is no in-process path and no mode knob (a stale
`SANDBOX_RENDER` value is ignored). The one optional knob, `SANDBOX_RENDER_URL`, is read with `getenv` at this call
site, never through `load_settings()`. An empty URL degrades the feature everywhere, Cloud Run and local alike: ERROR
logged once, and the returned runner raises `SandboxUnavailable` / `PuppeteerRunnerError`; nothing else degrades.

The factories build the runner (HTTP client, token cache) once and share it between every user.
Widget PNGs are not rendered by the sandbox (owner decision O16): `build_html_renderer` always builds the in-process
renderer when enabled; `SandboxHtmlRenderer` stays built but unwired.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from ..adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider, normalise_audience
from ..adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from ..adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from ..adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from ..domain.sandbox_errors import SandboxUnavailable
from ..ports.docx_runner_port import DocxRunnerPort
from ..ports.html_renderer_port import HtmlRendererPort
from ..ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from ..utils.logger import logger


class _UnavailableDocxRunner(DocxRunnerPort):
    """Stands in for the sandbox runner when no SANDBOX_RENDER_URL was configured."""

    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes:
        raise SandboxUnavailable("SANDBOX_RENDER_URL is empty")


class _UnavailablePuppeteerRunner(PuppeteerRunnerPort):
    """Stands in for the sandbox PDF runner when no SANDBOX_RENDER_URL was configured."""

    async def run(self, html_code: str, timeout: int) -> bytes:
        raise PuppeteerRunnerError("PDF rendering is unavailable (SandboxUnavailable); try again later")


def _sandbox_client_or_none(feature: str, getenv: Callable[..., Optional[str]]) -> Optional[SandboxHttpClient]:
    """The sandbox client, or None (ERROR logged) when SANDBOX_RENDER_URL is empty."""
    url = normalise_audience(getenv("SANDBOX_RENDER_URL") or "")
    if url:
        return SandboxHttpClient(url, MetadataIdTokenProvider(url))
    logger.error("[SandboxClient] SANDBOX_RENDER_URL is empty: %s rendering is unavailable until it is set", feature)
    return None


def build_docx_runner(
    getenv: Callable[..., Optional[str]] = os.getenv,
) -> DocxRunnerPort:
    client = _sandbox_client_or_none("DOCX", getenv)
    return _UnavailableDocxRunner() if client is None else SandboxDocxRunner(client)


def docx_runner_factory(
    getenv: Callable[..., Optional[str]] = os.getenv,
) -> Callable[[], DocxRunnerPort]:
    shared = build_docx_runner(getenv)  # logs the degraded ERROR once
    return lambda: shared


def build_pdf_runner(
    getenv: Callable[..., Optional[str]] = os.getenv,
) -> PuppeteerRunnerPort:
    client = _sandbox_client_or_none("PDF", getenv)
    return _UnavailablePuppeteerRunner() if client is None else SandboxPuppeteerRunner(client)


def pdf_runner_factory(
    getenv: Callable[..., Optional[str]] = os.getenv,
) -> Callable[[], PuppeteerRunnerPort]:
    shared = build_pdf_runner(getenv)
    return lambda: shared


def build_html_renderer(
    enabled: bool, inprocess_factory: Callable[[], HtmlRendererPort],
) -> Optional[HtmlRendererPort]:
    """None when the widget renderer is disabled; otherwise the in-process renderer
    (owner decision O16: widgets render in-process; `SandboxHtmlRenderer` is built but not wired)."""
    if not enabled:
        return None
    return inprocess_factory()
