"""
SandboxDocxRunner: DocxRunnerPort over the sandbox's `render.docx` op (RFC §4.7, §4.9, §4.10; plan G1, G10).

Frames `u32-be script_length | script | spec` and maps the outcome back to what `NodeDocxRunner` raises, with the
in-process wording, so the DocGenerator's repair loop is unchanged:
    ok                                                   -> DOCX bytes
    script_error / timeout / empty_output / too_large    -> DocxRunnerError(<text the service returned>)
    error, detail sandbox_lost                           -> DocxRunnerError("sandbox_lost: ...") (the input is the
                                                            model's script, two losses are charged to it: G10)
    bad_request (engine path, empty detail)              -> DocxRunnerError (a script can spoof exit 4; charged to it)
    bad_request unknown_op / bad_meta, any 404 / 405     -> SandboxMisconfigured (raised by the client: never the script)
    SandboxUnavailable / SandboxMisconfigured            -> propagate unchanged (systemic, nothing charged)
This module imports no document, image or archive library.
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
from ...domain.sandbox_docx_frame import frame_body
from ...ports.docx_runner_port import DocxRunnerError, DocxRunnerPort
from .sandbox_http_client import SandboxHttpClient

SANDBOX_LOST_TEXT = "sandbox_lost: the run ended without a result twice (the sandbox instance was lost)"
_FALLBACK_TEXT = {
    SandboxStatus.SCRIPT_ERROR: "script failed (no error text returned)",
    SandboxStatus.TIMEOUT: "Node.js process timed out",
    SandboxStatus.EMPTY_OUTPUT: "Node.js exited 0 but stdout is empty — no DOCX bytes produced",
    SandboxStatus.TOO_LARGE: "output exceeded the file size limit",
}


class SandboxDocxRunner(DocxRunnerPort):
    def __init__(self, client: SandboxHttpClient, *, memory_bytes: int = CEILING_MEMORY_BYTES,
                 cpu_s: float = CEILING_CPU_S, file_bytes: int = CEILING_FILE_BYTES) -> None:
        self._client = client
        self._memory_bytes = memory_bytes
        self._cpu_s = cpu_s
        self._file_bytes = file_bytes

    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes:
        limits = SandboxLimits(wall_s=float(timeout), memory_bytes=self._memory_bytes,
                               cpu_s=min(self._cpu_s, float(timeout)), file_bytes=self._file_bytes)
        body = frame_body(js_code.encode("utf-8"), spec_json.encode("utf-8"))
        reply = await self._client.call(SandboxOp.RENDER_DOCX, body, limits)
        status = reply.outcome.status
        if status == SandboxStatus.OK:
            return reply.body
        if status == SandboxStatus.ERROR and reply.outcome.detail == "sandbox_lost":
            raise DocxRunnerError(SANDBOX_LOST_TEXT)
        if status == SandboxStatus.BAD_REQUEST:
            raise DocxRunnerError("exit code 4\n(no stderr)")
        text = reply.body.decode("utf-8", errors="replace").strip()
        if status in _FALLBACK_TEXT:
            raise DocxRunnerError(text or _FALLBACK_TEXT[status])
        raise DocxRunnerError(f"sandbox error: {reply.outcome.detail or status.value}")
