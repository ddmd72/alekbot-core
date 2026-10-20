"""
Main-service side of the untrusted-content sandbox switch (docs/10_rfcs/UNTRUSTED_CONTENT_SANDBOX_RFC.md §4.9, §4.12).

Public interface:
    build_docx_runner(getenv=os.getenv, on_cloud_run=None) -> DocxRunnerPort   (one runner)
    docx_runner_factory(getenv=os.getenv, on_cloud_run=None) -> Callable[[], DocxRunnerPort]

`SANDBOX_RENDER` in {`inprocess` (default), `sandbox`}; anything else is a startup error (ValueError). Both knobs
(`SANDBOX_RENDER`, `SANDBOX_RENDER_URL`) are optional and read with `getenv` at this call site, never through
`load_settings()`. `sandbox` with an empty URL: on Cloud Run the service still starts (ERROR logged, the runner
raises `SandboxUnavailable`: the DOCX feature degrades, nothing else does); locally it is a startup error. There is
never an automatic fallback to the in-process runner (§4.12).

`docx_runner_factory` is what `UserAgentFactory.__init__` calls once, so a bad value stops the start and the sandbox
runner (HTTP client, token cache) or the degraded runner is built once and shared by every user. `inprocess` keeps
one `NodeDocxRunner` per build, as before.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from ..adapters.node_docx_runner import NodeDocxRunner
from ..adapters.sandbox_client.metadata_id_token_provider import MetadataIdTokenProvider, normalise_audience
from ..adapters.sandbox_client.sandbox_docx_runner import SandboxDocxRunner
from ..adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from ..domain.sandbox_errors import SandboxUnavailable
from ..ports.docx_runner_port import DocxRunnerPort
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
