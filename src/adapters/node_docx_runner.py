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

# Env vars the Node subprocess legitimately needs. Everything else (every
# application secret — API keys, OAuth/session secrets, service-account creds)
# is deliberately withheld: the script is LLM-generated and must be treated as
# untrusted. A docx build needs only a PATH to locate `node`/node_modules.
_SAFE_ENV_KEYS = ("PATH", "HOME", "NODE_PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")

# Prepended to every LLM-generated script. Blocks the core modules that enable
# network egress (→ GCP metadata server → service-account token → full DB/GCS
# access) and process spawning (→ RCE), while leaving the docx toolchain
# (fs/path/zlib/stream/buffer) untouched. Combined with _SAFE_ENV_KEYS this
# collapses the blast radius of a malicious generated script to "produce a bad
# docx", not "exfiltrate secrets".
_SECURITY_PRELUDE = """\
'use strict';
(function () {
  const Module = require('module');
  const BLOCKED = new Set([
    'child_process', 'cluster', 'worker_threads', 'inspector', 'repl', 'v8',
    'http', 'http2', 'https', 'net', 'tls', 'dns', 'dgram'
  ]);
  const _load = Module._load;
  Module._load = function (request) {
    const name = String(request).replace(/^node:/, '');
    if (BLOCKED.has(name)) {
      throw new Error('Blocked module for security: ' + request);
    }
    return _load.apply(this, arguments);
  };
  // Node 18+ exposes a global fetch() that bypasses the require() hook above.
  const blockedFetch = function () {
    throw new Error('Network access (fetch) is blocked for security');
  };
  try { globalThis.fetch = blockedFetch; } catch (e) { /* read-only: ignore */ }
})();
"""


def _safe_subprocess_env() -> dict:
    """Minimal allow-listed environment for the untrusted Node subprocess."""
    env = {}
    for key in _SAFE_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


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
            # Untrusted (LLM-generated) script — prepend the security prelude that
            # disables network/process-spawning core modules before any user code runs.
            tmp.write(_SECURITY_PRELUDE + "\n" + js_code)
            tmp.flush()
            tmp.close()

            logger.debug("NodeDocxRunner: executing %s", tmp.name)

            proc = await asyncio.create_subprocess_exec(
                "node", tmp.name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_safe_subprocess_env(),
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
                logger.debug("Failed to remove temp script file %s", tmp.name)
