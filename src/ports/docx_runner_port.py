"""
DocxRunnerPort
==============

Port for executing a Node.js DOCX generation script.
System boundary: subprocess (local Node.js) or Cloud Function (future).

Implementations:
  NodeDocxRunner  — asyncio subprocess calling local `node` binary.
  (future)        — Cloud Function HTTP call.
"""

from abc import ABC, abstractmethod


class DocxRunnerError(Exception):
    """Raised by DocxRunnerPort implementations on execution failure."""


class DocxRunnerPort(ABC):

    @abstractmethod
    async def run(self, js_code: str, spec_json: str, timeout: int) -> bytes:
        """
        Execute a Node.js script that generates a DOCX file.

        Args:
            js_code:   Complete, executable Node.js script. Must read spec JSON
                       from process.stdin and write raw DOCX bytes to process.stdout.
            spec_json: JSON string piped to the script's stdin.
            timeout:   Execution timeout in seconds.

        Returns:
            Raw DOCX bytes from the script's stdout.

        Raises:
            DocxRunnerError: on non-zero exit, empty stdout, or timeout.
        """
