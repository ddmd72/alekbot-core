"""
Script-hardening constants shared by the in-process DOCX runner and the sandbox render.docx engine
(plan G3: one copy, no adapter-to-adapter import). Pure strings; the environment itself is read by the adapters.
"""

# Env vars the Node subprocess legitimately needs. Everything else (every
# application secret — API keys, OAuth/session secrets, service-account creds)
# is deliberately withheld: the script is LLM-generated and must be treated as
# untrusted. A docx build needs only a PATH to locate `node`/node_modules.
SAFE_ENV_KEYS = ("PATH", "HOME", "NODE_PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")

# Prepended to every LLM-generated script. Blocks the core modules that enable
# network egress (→ GCP metadata server → service-account token → full DB/GCS
# access), process spawning (→ RCE), and local filesystem access (→ reading
# container files into the output doc), while leaving the docx toolchain
# (path/zlib/stream/buffer) untouched. The docx lib builds via Packer.toBuffer
# (in-memory → stdout) and does NOT require `fs` — verified empirically before
# adding the block. Combined with SAFE_ENV_KEYS this collapses the blast radius
# of a malicious generated script to "produce a bad docx", not "exfiltrate".
SECURITY_PRELUDE = """\
'use strict';
(function () {
  const Module = require('module');
  const BLOCKED = new Set([
    'child_process', 'cluster', 'worker_threads', 'inspector', 'repl', 'v8',
    'http', 'http2', 'https', 'net', 'tls', 'dns', 'dgram',
    'fs', 'fs/promises'
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
