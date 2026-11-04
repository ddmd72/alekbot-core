"""
Main-service side of the untrusted-content sandbox switch (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.9, §4.12).

Public interface:
    build_docx_runner(getenv=os.getenv, on_cloud_run=None) -> DocxRunnerPort   (one runner)
    docx_runner_factory(getenv=os.getenv, on_cloud_run=None) -> Callable[[], DocxRunnerPort]
    build_pdf_runner(getenv=os.getenv, on_cloud_run=None) -> PuppeteerRunnerPort
    pdf_runner_factory(getenv=os.getenv, on_cloud_run=None) -> Callable[[], PuppeteerRunnerPort]
    build_html_renderer(enabled, inprocess_factory, getenv=os.getenv, on_cloud_run=None) -> Optional[HtmlRendererPort]

`SANDBOX_RENDER` in {`inprocess` (default), `sandbox`}; anything else is a startup error (ValueError). Both knobs
(`SANDBOX_RENDER`, `SANDBOX_RENDER_URL`) are optional and read with `getenv` at this call site, never through
`load_settings()`. `sandbox` with an empty URL: on Cloud Run the service still starts (ERROR logged, the runner
raises `SandboxUnavailable`: the DOCX feature degrades, nothing else does); locally it is a startup error. There is
never an automatic fallback to the in-process runner (§4.12).

`docx_runner_factory` is what `UserAgentFactory.__init__` calls once, so a bad value stops the start and the sandbox
runner (HTTP client, token cache) or the degraded runner is built once and shared by every user. `inprocess` keeps
one `NodeDocxRunner` per build, as before.

One knob for the whole render family (owner decision O15): the same `SANDBOX_RENDER` / `SANDBOX_RENDER_URL` also
select the PDF runner (`SandboxPuppeteerRunner` / `NodePuppeteerRunner`) and the widget PNG renderer
(`SandboxHtmlRenderer` / the in-process factory's object). Same rules: bad value or local empty URL is a ValueError,
Cloud Run with an empty URL degrades that feature (ERROR logged once), never a fallback between the two.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from ..adapters.node_docx_runner import NodeDocxRunner
from ..adapters.node_puppeteer_runner import NodePuppeteerRunner
from ..adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider, normalise_audience
from ..adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from ..adapters.sandbox_client.sandbox_html_renderer import SandboxHtmlRenderer
from ..adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from ..adapters.sandbox_client.sandbox_puppeteer_runner import SandboxPuppeteerRunner
from ..domain.sandbox_errors import SandboxUnavailable
from ..ports.docx_runner_port import DocxRunnerPort
from ..ports.html_renderer_port import HtmlRenderError, HtmlRendererPort
from ..ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from ..utils.cloud_run import on_cloud_run as is_on_cloud_run
from ..utils.logger import logger

MODE_INPROCESS = "inprocess"
MODE_SANDBOX = "sandbox"


class _UnavailableDocxRunner(DocxRunnerPort):
    """Stands in for the sandbox runner when `sandbox` was asked for but no URL was configured."""

    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes:
        raise SandboxUnavailable("SANDBOX_RENDER=sandbox but SANDBOX_RENDER_URL is empty")


def build_docx_runner(
    getenv: Callable[..., Optional[str]] = os.getenv, on_cloud_run: Optional[bool] = None,
) -> DocxRunnerPort:
    if on_cloud_run is None:
        on_cloud_run = is_on_cloud_run(getenv)
    mode = (getenv("SANDBOX_RENDER") or MODE_INPROCESS).strip().lower()
    if mode == MODE_INPROCESS:
        return NodeDocxRunner()
    if mode != MODE_SANDBOX:
        logger.error("[SandboxClient] unknown SANDBOX_RENDER value; expected 'sandbox' or 'inprocess'")
        raise ValueError("SANDBOX_RENDER must be 'sandbox' or 'inprocess'")
    url = normalise_audience(getenv("SANDBOX_RENDER_URL") or "")
    if not url:
        if not on_cloud_run:
            logger.error("[SandboxClient] SANDBOX_RENDER=sandbox needs SANDBOX_RENDER_URL")
            raise ValueError("SANDBOX_RENDER=sandbox needs SANDBOX_RENDER_URL")
        logger.error("[SandboxClient] SANDBOX_RENDER=sandbox but SANDBOX_RENDER_URL is empty: "
                     "DOCX rendering is unavailable until it is set")
        return _UnavailableDocxRunner()
    return SandboxDocxRunner(SandboxHttpClient(url, MetadataIdTokenProvider(url)))


def docx_runner_factory(
    getenv: Callable[..., Optional[str]] = os.getenv, on_cloud_run: Optional[bool] = None,
) -> Callable[[], DocxRunnerPort]:
    mode = (getenv("SANDBOX_RENDER") or MODE_INPROCESS).strip().lower()
    if mode == MODE_INPROCESS:
        return NodeDocxRunner
    shared = build_docx_runner(getenv, on_cloud_run)  # raises on a bad value; logs the degraded ERROR once
    return lambda: shared


class _UnavailablePuppeteerRunner(PuppeteerRunnerPort):
    """Stands in for the sandbox PDF runner when `sandbox` was asked for but no URL was configured."""

    async def run(self, html_code: str, timeout: int) -> bytes:
        raise PuppeteerRunnerError("PDF rendering is unavailable (SandboxUnavailable); try again later")


class _UnavailableHtmlRenderer(HtmlRendererPort):
    """Stands in for the sandbox PNG renderer when `sandbox` was asked for but no URL was configured."""

    async def render(self, html: str, width: int = 480) -> bytes:
        raise HtmlRenderError("PNG rendering is unavailable (SandboxUnavailable); try again later")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _sandbox_client_or_none(
    feature: str, getenv: Callable[..., Optional[str]], on_cloud_run: Optional[bool],
) -> Optional[SandboxHttpClient]:
    """The shared client for `sandbox` mode; None when the URL is empty on Cloud Run (ERROR logged); ValueError
    for a local empty URL. Call only when the mode is already known to be `sandbox`."""
    if on_cloud_run is None:
        on_cloud_run = is_on_cloud_run(getenv)
    url = normalise_audience(getenv("SANDBOX_RENDER_URL") or "")
    if url:
        return SandboxHttpClient(url, MetadataIdTokenProvider(url))
    if not on_cloud_run:
        logger.error("[SandboxClient] SANDBOX_RENDER=sandbox needs SANDBOX_RENDER_URL")
        raise ValueError("SANDBOX_RENDER=sandbox needs SANDBOX_RENDER_URL")
    logger.error("[SandboxClient] SANDBOX_RENDER=sandbox but SANDBOX_RENDER_URL is empty: "
                 "%s rendering is unavailable until it is set", feature)
    return None


def _mode(getenv: Callable[..., Optional[str]]) -> str:
    mode = (getenv("SANDBOX_RENDER") or MODE_INPROCESS).strip().lower()
    if mode not in (MODE_INPROCESS, MODE_SANDBOX):
        logger.error("[SandboxClient] unknown SANDBOX_RENDER value; expected 'sandbox' or 'inprocess'")
        raise ValueError("SANDBOX_RENDER must be 'sandbox' or 'inprocess'")
    return mode


def build_pdf_runner(
    getenv: Callable[..., Optional[str]] = os.getenv, on_cloud_run: Optional[bool] = None,
) -> PuppeteerRunnerPort:
    if _mode(getenv) == MODE_INPROCESS:
        return NodePuppeteerRunner()
    client = _sandbox_client_or_none("PDF", getenv, on_cloud_run)
    return _UnavailablePuppeteerRunner() if client is None else SandboxPuppeteerRunner(client)


def pdf_runner_factory(
    getenv: Callable[..., Optional[str]] = os.getenv, on_cloud_run: Optional[bool] = None,
) -> Callable[[], PuppeteerRunnerPort]:
    if _mode(getenv) == MODE_INPROCESS:
        return NodePuppeteerRunner
    shared = build_pdf_runner(getenv, on_cloud_run)
    return lambda: shared


def build_html_renderer(
    enabled: bool, inprocess_factory: Callable[[], HtmlRendererPort],
    getenv: Callable[..., Optional[str]] = os.getenv, on_cloud_run: Optional[bool] = None,
) -> Optional[HtmlRendererPort]:
    """None when the widget renderer is disabled (in every mode); otherwise the object the knob selects."""
    if not enabled:
        return None
    if _mode(getenv) == MODE_INPROCESS:
        return inprocess_factory()
    client = _sandbox_client_or_none("PNG", getenv, on_cloud_run)
    return _UnavailableHtmlRenderer() if client is None else SandboxHtmlRenderer(client)
