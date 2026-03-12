"""
NodeDocxRunner
==============

DocxRunnerPort implementation backed by a local `node` subprocess.

The Node.js script is written to a temp file inside docx_generator/ so that
node_modules resolution works correctly. Temp file is always deleted in the
finally block regardless of success or failure.
"""

import asyncio
import os
import tempfile

from ..ports.docx_runner_port import DocxRunnerError, DocxRunnerPort
from ..utils.logger import logger


# docx_generator/ directory at project root — Node.js resolves node_modules from here.
_DOCX_GENERATOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docx_generator")
)


class NodeDocxRunner(DocxRunnerPort):
    """Runs a Node.js script via asyncio subprocess and returns DOCX bytes."""

    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes:
        os.makedirs(_DOCX_GENERATOR_DIR, exist_ok=True)

        tmp = tempfile.NamedTemporaryFile(
            suffix=".js",
            mode="w",
            encoding="utf-8",
            dir=_DOCX_GENERATOR_DIR,
            delete=False,
        )
        try:
            tmp.write(js_code)
            tmp.flush()
            tmp.close()

            logger.debug("NodeDocxRunner: executing %s", tmp.name)

            proc = await asyncio.create_subprocess_exec(
                "node", tmp.name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=spec_json.encode("utf-8")),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise DocxRunnerError(f"Node.js process timed out after {timeout}s")

            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                raise DocxRunnerError(
                    f"exit code {proc.returncode}\n{stderr_text or '(no stderr)'}"
                )

            if stderr_text:
                logger.debug("NodeDocxRunner: Node.js stderr (non-fatal):\n%s", stderr_text)

            if not stdout:
                raise DocxRunnerError(
                    "Node.js exited 0 but stdout is empty — no DOCX bytes produced"
                )

            return stdout

        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
