"""
DocxRenderEngine: the service's `render.docx` engine (RFC §4.6, §4.7, §4.9; plan G1, G2, G20).

Not `NodeDocxRunner` itself: that runner writes into `docx_generator/`, runs node as the caller's uid and applies no
limits. This engine keeps its observable contract (prelude, error texts) but runs `render_docx_child` through the
`ChildLauncher` (own uid, allowlisted environment, `limits_exec` applied once before the child starts and inherited by
node across its exec).

The server never reads node's stdout through a pipe: the child points node's stdout at `out.bin` in the workdir, and
this engine only checks that file's size. The one text decoded from the child is the script's error text (the model's own
script output, §4.9), read from `err.txt` and bounded by ERROR_TEXT_CAP_BYTES. The launcher's own stderr summary
(length + SHA-256) covers failures of the Python child itself.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Optional

from ...domain.sandbox_contract import (
    ERROR_TEXT_CAP_BYTES, RESPONSE_CAP_BYTES, SandboxEngineResult, SandboxLimits, SandboxStatus,
)
from ...utils.logger import logger
from .child_launcher import ChildLauncher
from .render_docx_child import (
    ERROR_FILE, EXIT_BAD_REQUEST, OUTPUT_FILE,
)

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "render_docx_child.py")
_TRUNCATION_MARKER = "\n...[truncated]"
EMPTY_OUTPUT_TEXT = "Node.js exited 0 but stdout is empty — no DOCX bytes produced"


class DocxRenderEngine:
    def __init__(self, launcher: ChildLauncher, *, python: str = sys.executable,
                 response_cap_bytes: int = RESPONSE_CAP_BYTES) -> None:
        self._launcher = launcher
        self._python = python
        self._response_cap = response_cap_bytes

    async def run(self, body_path: str, workdir: str, limits: SandboxLimits) -> SandboxEngineResult:
        started = time.monotonic()
        child = await self._launcher.run(
            [self._python, "-B", "-E", "-s", _CHILD_SCRIPT, body_path, workdir],
            workdir=workdir, env={}, limits=limits,
            stdin_path=os.devnull, stdout_path=os.devnull, op="render_docx",
        )
        wall_s = time.monotonic() - started

        def result(status: SandboxStatus, detail: str = "", output_path: Optional[str] = None) -> SandboxEngineResult:
            return SandboxEngineResult(status=status, output_path=output_path, detail=detail,
                                       peak_rss_bytes=child.peak_rss_bytes, wall_s=wall_s)

        if child.timed_out:
            return result(SandboxStatus.TIMEOUT, f"Node.js process timed out after {limits.wall_s:g}s")
        if child.returncode == -signal.SIGXFSZ:
            return result(SandboxStatus.TOO_LARGE, "output exceeded the file size limit")
        error_path = os.path.join(workdir, ERROR_FILE)
        if child.returncode != 0:
            if not os.path.exists(error_path):   # the Python child failed before node ran
                if child.returncode == EXIT_BAD_REQUEST:
                    return result(SandboxStatus.BAD_REQUEST, "request body or limits could not be used")
                return result(SandboxStatus.ERROR, f"engine child failed (exit code {child.returncode})")
            return result(SandboxStatus.SCRIPT_ERROR, _error_text(child.returncode, error_path))
        output_path = os.path.join(workdir, OUTPUT_FILE)
        size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        if size == 0:
            return result(SandboxStatus.EMPTY_OUTPUT, EMPTY_OUTPUT_TEXT)
        if size > self._response_cap:
            logger.warning(f"[Sandbox] render.docx output {size} bytes exceeds the cap {self._response_cap}")
            return result(SandboxStatus.TOO_LARGE, "output exceeded the response cap")
        return result(SandboxStatus.OK, output_path=output_path)


def _error_text(returncode: int, error_path: str) -> str:
    """`exit code N\\n<stderr or "(no stderr)">`, the in-process format, bounded by ERROR_TEXT_CAP_BYTES."""
    with open(error_path, "rb") as handle:
        raw = handle.read(ERROR_TEXT_CAP_BYTES + 1)
    text = f"exit code {returncode}\n{raw.decode('utf-8', errors='replace').strip() or '(no stderr)'}"
    encoded = text.encode("utf-8")
    if len(encoded) <= ERROR_TEXT_CAP_BYTES:
        return text
    keep = ERROR_TEXT_CAP_BYTES - len(_TRUNCATION_MARKER.encode())
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
