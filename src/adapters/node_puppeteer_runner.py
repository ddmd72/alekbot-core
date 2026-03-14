"""
NodePuppeteerRunner
===================

PuppeteerRunnerPort implementation backed by a local Node.js subprocess.

Executes the fixed pdf_generator/runner.js wrapper, piping HTML via stdin
and capturing PDF bytes from stdout. The runner.js is not LLM-generated —
it is a fixed Puppeteer wrapper that accepts any valid HTML document.
"""

import asyncio
import os

from ..ports.puppeteer_runner_port import PuppeteerRunnerError, PuppeteerRunnerPort
from ..utils.logger import logger


# pdf_generator/ directory at project root — Node.js resolves node_modules from here.
_PDF_GENERATOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pdf_generator")
)
_RUNNER_SCRIPT = os.path.join(_PDF_GENERATOR_DIR, "runner.js")


class NodePuppeteerRunner(PuppeteerRunnerPort):
    """Renders HTML to PDF via the fixed pdf_generator/runner.js subprocess."""

    async def run(self, html_code: str, timeout: int) -> bytes:
        if not os.path.isfile(_RUNNER_SCRIPT):
            raise PuppeteerRunnerError(
                f"pdf_generator/runner.js not found at {_RUNNER_SCRIPT}. "
                "Run 'npm install' in pdf_generator/ first."
            )

        logger.debug("NodePuppeteerRunner: executing %s", _RUNNER_SCRIPT)

        proc = await asyncio.create_subprocess_exec(
            "node", _RUNNER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_PDF_GENERATOR_DIR,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=html_code.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise PuppeteerRunnerError(f"Puppeteer process timed out after {timeout}s")

        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise PuppeteerRunnerError(
                f"exit code {proc.returncode}\n{stderr_text or '(no stderr)'}"
            )

        if stderr_text:
            logger.debug("NodePuppeteerRunner: stderr (non-fatal):\n%s", stderr_text)

        if not stdout:
            raise PuppeteerRunnerError(
                "Puppeteer exited 0 but stdout is empty — no PDF bytes produced"
            )

        logger.debug("NodePuppeteerRunner: PDF generated, %d bytes", len(stdout))
        return stdout
