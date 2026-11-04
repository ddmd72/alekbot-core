"""
SandboxPuppeteerRunner: PuppeteerRunnerPort over the sandbox's `render.pdf` op (RFC §4.7).

The body is the HTML as UTF-8 bytes; every sandbox outcome becomes a `PuppeteerRunnerError` whose text matches the
in-process runner where one exists. The systemic `SandboxUnavailable` / `SandboxMisconfigured` become a
`PuppeteerRunnerError` too (the PDF agent has no repair loop), chained with `from exc`.
"""
from __future__ import annotations

from ...domain.sandbox_contract import (
    CEILING_CPU_S,
    CEILING_FILE_BYTES,
    CEILING_MEMORY_BYTES,
    SandboxLimits,
    SandboxOp,
    SandboxStatus,
)
from ...domain.sandbox_errors import SandboxMisconfigured, SandboxUnavailable
from ...ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from .sandbox_http_client import SandboxHttpClient
from .sandbox_render_outcome import RenderTexts, failure_text, unavailable_text


class SandboxPuppeteerRunner(PuppeteerRunnerPort):
    def __init__(self, client: SandboxHttpClient, *, memory_bytes: int = CEILING_MEMORY_BYTES,
                 cpu_s: float = CEILING_CPU_S, file_bytes: int = CEILING_FILE_BYTES) -> None:
        self._client = client
        self._memory_bytes = memory_bytes
        self._cpu_s = cpu_s
        self._file_bytes = file_bytes

    async def run(self, html_code: str, timeout: int) -> bytes:
        limits = SandboxLimits(wall_s=float(timeout), memory_bytes=self._memory_bytes,
                               cpu_s=min(self._cpu_s, float(timeout)), file_bytes=self._file_bytes)
        try:
            reply = await self._client.call(SandboxOp.RENDER_PDF, html_code.encode("utf-8"), limits)
        except (SandboxUnavailable, SandboxMisconfigured) as exc:
            raise PuppeteerRunnerError(unavailable_text("PDF", exc)) from exc
        if reply.outcome.status == SandboxStatus.OK:
            return reply.body
        raise PuppeteerRunnerError(failure_text(reply, RenderTexts(
            timeout=f"Puppeteer process timed out after {timeout}s",
            empty_output="Puppeteer exited 0 but stdout is empty — no PDF bytes produced",
            script_error="PDF render failed in the sandbox",
            too_large="PDF exceeded the size limit",
            bad_request="PDF render refused the request (bad_request)")))
