"""
render.docx engine child (RFC §4.6, §4.9; plan G1, G5): runs under `limits_exec`, as the engine uid.

    python -B -I <path>/render_docx_child.py <body-file> <workdir>

Body framing (G1): `u32-be script_length | script | spec`. The server writes the body to a file and never splits it; this
child does. It writes `script.js` (the shared security prelude, then the model's script) and `spec.json` into the workdir,
then replaces itself with `node script.js`, so the limits inherited from `limits_exec` carry over:
stdin = spec.json, stdout = out.bin, stderr = err.txt (all in the workdir). The DOCX bytes therefore never pass through a
pipe the server interprets, and err.txt is the one place a child's stderr comes back (the model's own script output, §4.9).
Module resolution relies on NODE_PATH (G5), because the script lives in the workdir, not in `docx_generator/`.

Started by file path, not `-m`: `limits_exec` has already changed into the workdir, where `src` cannot be found, so the
module appends the repository root to sys.path itself (last, under `-I`, which also keeps the script's own directory
and the cwd off the path) before importing the shared prelude.

Exit codes: EXIT_BAD_REQUEST = unreadable body or arguments; EXIT_EXEC_FAILED = `node` could not be started. Anything else
is node's own exit status.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Sequence

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)   # last: nothing in the repo root can shadow a stdlib module imported after this

from src.domain.docx_script_prelude import SAFE_ENV_KEYS, SECURITY_PRELUDE  # noqa: E402
from src.domain.sandbox_docx_frame import BadBody, split_body  # noqa: E402

EXIT_BAD_REQUEST = 4    # same meaning as limits_exec / drive_parser.child
EXIT_EXEC_FAILED = 5

SCRIPT_FILE = "script.js"
SPEC_FILE = "spec.json"
OUTPUT_FILE = "out.bin"
ERROR_FILE = "err.txt"

_NODE_MODULES = os.path.join(_REPO_ROOT, "docx_generator", "node_modules")


def build_script(user_script: bytes) -> bytes:
    return SECURITY_PRELUDE.encode("utf-8") + b"\n" + user_script


def node_env() -> Dict[str, str]:
    """Allow-listed environment for node (the same names as the in-process runner), plus the module path."""
    env = {key: value for key in SAFE_ENV_KEYS if (value := os.environ.get(key))}
    env["NODE_PATH"] = _NODE_MODULES
    return env


def prepare(body_path: str, workdir: str) -> None:
    with open(body_path, "rb") as handle:
        script, spec = split_body(handle.read())
    with open(os.path.join(workdir, SCRIPT_FILE), "wb") as handle:
        handle.write(build_script(script))
    with open(os.path.join(workdir, SPEC_FILE), "wb") as handle:
        handle.write(spec)


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("render_docx_child: expected <body-file> <workdir>\n")
        return EXIT_BAD_REQUEST
    body_path, workdir = argv
    try:
        prepare(body_path, workdir)
    except (OSError, BadBody) as exc:
        sys.stderr.write(f"render_docx_child: {type(exc).__name__}\n")
        return EXIT_BAD_REQUEST
    try:
        stdin_fd = os.open(os.path.join(workdir, SPEC_FILE), os.O_RDONLY)
        stdout_fd = os.open(os.path.join(workdir, OUTPUT_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        stderr_fd = os.open(os.path.join(workdir, ERROR_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        os.dup2(stdin_fd, 0)
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        node_argv: List[str] = ["node", os.path.join(workdir, SCRIPT_FILE)]
        os.execvpe("node", node_argv, node_env())
    except OSError as exc:
        sys.stderr.write(f"render_docx_child: node could not be started: {type(exc).__name__}\n")
        return EXIT_EXEC_FAILED
    return EXIT_EXEC_FAILED   # unreachable: execvpe replaces the process


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
