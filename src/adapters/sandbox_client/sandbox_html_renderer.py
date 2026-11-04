"""
SandboxHtmlRenderer: HtmlRendererPort over the sandbox's `render.png` op (RFC §4.7).

The body is `u32-be width | html` (`frame_png_body`); every sandbox outcome becomes an `HtmlRenderError`, the systemic
`SandboxUnavailable` / `SandboxMisconfigured` included (the widget caller drops the widget on any error).
`start()` is a no-op (no local browser); `stop()` closes the client.
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
from ...domain.sandbox_png_frame import BadPngBody, frame_png_body
from ...ports.html_renderer_port import HtmlRenderError, HtmlRendererPort
from .sandbox_http_client import SandboxHttpClient
from .sandbox_render_outcome import RenderTexts, failure_text, unavailable_text

PNG_WALL_S = 30.0


class SandboxHtmlRenderer(HtmlRendererPort):
    def __init__(self, client: SandboxHttpClient, *, wall_s: float = PNG_WALL_S,
                 memory_bytes: int = CEILING_MEMORY_BYTES, cpu_s: float = CEILING_CPU_S,
                 file_bytes: int = CEILING_FILE_BYTES) -> None:
        self._client = client
        self._wall_s = wall_s
        self._memory_bytes = memory_bytes
        self._cpu_s = cpu_s
        self._file_bytes = file_bytes

    async def render(self, html: str, width: int = 480) -> bytes:
        try:
            body = frame_png_body(width, html.encode("utf-8"))
        except BadPngBody as exc:
            raise HtmlRenderError(f"render failed: {exc}") from exc
        limits = SandboxLimits(wall_s=self._wall_s, memory_bytes=self._memory_bytes,
                               cpu_s=min(self._cpu_s, self._wall_s), file_bytes=self._file_bytes)
        try:
            reply = await self._client.call(SandboxOp.RENDER_PNG, body, limits)
        except (SandboxUnavailable, SandboxMisconfigured) as exc:
            raise HtmlRenderError(unavailable_text("PNG", exc)) from exc
        if reply.outcome.status == SandboxStatus.OK:
            return reply.body
        raise HtmlRenderError(failure_text(reply, RenderTexts(
            timeout=f"PNG render timed out after {self._wall_s:g}s",
            empty_output="PNG render produced no output",
            script_error="PNG render failed in the sandbox",
            too_large="PNG exceeded the size limit",
            bad_request="PNG render refused the request (bad_request)")))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._client.aclose()
